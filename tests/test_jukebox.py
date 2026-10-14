import io
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from analysis import Analyzer, Interrupted, parse_log, peaks_from_pcm
from core import Engine, Store, auto_gain
from player import Player
from sounds import ReadySounds, chime_wav
from reader import Reader, crc_a
from fastapi.testclient import TestClient

from server import create_app, status_key

JUKEBOX = {"X-Jukebox": "1"}


class FakePlayer:
    def __init__(self):
        self.events = []
        self.ended = False
        self.elapsed = 0.0
        self.position = None
        self.duration = None

    def stop(self):
        self.events.append("stop")

    def play(self, path, start=0.0, end=None, gain_db=0.0):
        self.events.append(Path(path).name)
        self.last = {"start": start, "end": end, "gain_db": gain_db}

    def seek(self, position):
        self.events.append(("seek", position))

    def poll(self):
        if self.ended:
            self.ended = False
            return "eof"
        return None

    def volume(self, value):
        self.events.append(value)

    def close(self):
        pass


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = self.open_store()
        for name in ("a", "b"):
            (self.store.root / "music" / (name + ".mp3")).write_bytes(b"fake")
            self.store.add_track(name, name, name + ".mp3", name + ".mp3")
        self.store.bind_tag("TAG_A", "a")
        self.store.bind_tag("TAG_B", "b")
        self.player = FakePlayer()
        self.now = 0.0
        self.engine = Engine(self.store, self.player, lambda: self.now)

    def test_glitches_do_not_restart_and_removal_stops(self):
        self.engine.tick("TAG_A", 1)
        self.engine.tick(None, 1.4)
        self.engine.tick("TAG_A", 1.6)
        self.assertEqual(self.player.events, ["stop", "a.mp3"])
        self.engine.tick(None, 3)
        self.assertIsNone(self.engine.current)
        self.assertEqual(self.player.events[-1], "stop")

    def test_direct_swap_stops_previous_track_first(self):
        self.engine.tick("TAG_A", 1)
        self.engine.tick("TAG_B", 1.15)
        self.assertEqual(self.player.events, ["stop", "a.mp3", "stop", "b.mp3"])

    def test_unknown_tag_stops_old_track(self):
        self.engine.tick("TAG_A", 1)
        self.engine.tick("UNKNOWN", 1.15)
        self.assertIsNone(self.engine.track)
        self.assertIn("Неизвестная", self.engine.message)

    def scan_ready(self):
        self.engine.arm("b")
        self.engine.tick(None, 0)
        self.engine.tick(None, 1.3)

    def test_binding_requires_removal_first(self):
        self.engine.arm("b")
        self.engine.tick("TAG_A", 1)
        self.engine.tick(None, 1.3)
        self.engine.tick("TAG_A", 1.6)
        self.assertEqual(self.engine.pending["phase"], "remove")
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"]["track"], "a")

    def test_conflict_needs_exact_confirmation_and_persists(self):
        self.scan_ready()
        self.engine.tick("TAG_A", 1.5)
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"]["track"], "a")
        with self.assertRaises(ValueError):
            self.engine.confirm("TAG_B", "b")
        self.engine.confirm("TAG_A", "b")
        self.assertEqual(self.open_store().snapshot()["tags"]["TAG_A"]["track"], "b")

    def test_new_binding_and_cancel_do_not_autoplay(self):
        self.scan_ready()
        self.now = 1.5
        self.engine.tick("NEW", 1.5)
        self.assertEqual(self.store.snapshot()["tags"]["NEW"]["track"], "b")
        self.engine.tick("NEW", 1.6)
        self.assertNotIn("b.mp3", self.player.events)

    def test_binding_timeout(self):
        self.scan_ready()
        self.engine.tick("TAG_A", 1.5)
        self.now = 121
        with self.assertRaises(ValueError):
            self.engine.confirm("TAG_A", "b")
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"]["track"], "a")

    def open_store(self):
        store = Store(self.temp.name)
        self.addCleanup(store.db.close)
        return store

    def test_failed_write_rolls_back(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.bind_tag("TAG_A", "no-such-track")  # foreign key violation
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"]["track"], "a")
        self.assertEqual(self.open_store().snapshot()["tags"]["TAG_A"]["track"], "a")
    def test_eof_repeats_with_tag(self):
        self.engine.tick("TAG_A", 1)
        self.player.ended = True
        self.engine.tick("TAG_A", 1.2)
        self.assertEqual(self.player.events.count("a.mp3"), 2)
        self.assertEqual(self.engine.status()["playing_track"], "a")

    def test_manual_play_without_tag_runs_once(self):
        self.engine.manual_play("a")
        self.player.ended = True
        self.engine.tick(None, 1)
        self.engine.tick(None, 2)
        self.assertEqual(self.player.events.count("a.mp3"), 1)
        self.assertIsNone(self.engine.track)

    def test_stop_suppresses_present_tag_until_reinserted(self):
        self.engine.tick("TAG_A", 1)
        self.engine.manual_stop()
        self.engine.tick("TAG_A", 1.2)
        self.engine.tick("TAG_A", 1.4)
        self.assertIsNone(self.engine.track)
        self.engine.tick(None, 2.5)
        self.engine.tick("TAG_A", 2.7)
        self.assertEqual(self.player.events.count("a.mp3"), 2)

    def test_manual_stop_can_be_overridden_by_play(self):
        self.engine.tick("TAG_A", 1)
        self.engine.manual_stop()
        self.engine.manual_play("a")
        self.player.ended = True
        self.engine.tick("TAG_A", 1.3)
        self.assertEqual(self.engine.track, "a")
        self.assertEqual(self.player.events.count("a.mp3"), 3)

    def test_tag_overrides_manual_play(self):
        self.engine.manual_play("a")
        self.engine.tick("TAG_B", 1)
        self.assertEqual(self.engine.track, "b")
        self.assertEqual(self.engine.source, "tag")

    def test_eof_inside_grace_repeats_then_removal_stops(self):
        self.engine.tick("TAG_A", 1)
        self.player.ended = True
        self.engine.tick(None, 1.8)
        self.assertEqual(self.engine.track, "a")
        self.engine.tick(None, 2.1)
        self.assertIsNone(self.engine.track)

    def test_reappearance_after_grace_restarts_even_without_empty_poll(self):
        self.engine.tick("TAG_A", 1)
        self.engine.tick("TAG_A", 2.1)
        self.assertEqual(self.player.events.count("a.mp3"), 2)

    def test_default_settings(self):
        self.assertEqual(self.store.snapshot()["settings"],
                         {"volume": 35, "read_grace_ms": 1000, "continue_playback": False,
                          "normalize": True, "target_lufs": -16.0, "ready_sound": True, "ready_sound_file": None})
    def test_player_error_visible_and_not_retried_forever(self):
        self.engine.tick("TAG_A", 1)
        with patch.object(self.player, "poll", side_effect=RuntimeError("ALSA unavailable")):
            self.engine.tick("TAG_A", 1.1)
        self.engine.tick("TAG_A", 1.2)
        self.assertIsNone(self.engine.status()["playing_track"])
        self.assertEqual(self.engine.status()["audio_error"], "ALSA unavailable")
        self.assertEqual(self.player.events.count("a.mp3"), 1)

    def test_seek_goes_to_player_and_needs_a_track(self):
        with self.assertRaises(ValueError):
            self.engine.seek(10)
        self.engine.tick("TAG_A", 1)
        self.engine.seek(-5)
        self.assertEqual(self.player.events[-1], ("seek", 0.0))

    def test_ui_stop_survives_brief_tag_absence(self):
        self.engine.tick("TAG_A", 1)
        self.engine.manual_stop()
        self.engine.tick(None, 1.4)
        self.engine.tick("TAG_A", 1.6)
        self.assertIsNone(self.engine.track)


    def test_unbind_keeps_labelled_disc(self):
        self.engine.set_label("TAG_A", "  красно-жёлтый ")
        self.engine.unbind("TAG_A")
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"], {"track": None, "label": "красно-жёлтый"})
        self.engine.unbind("TAG_B")
        self.assertNotIn("TAG_B", self.store.snapshot()["tags"])
        self.engine.tick("TAG_A", 1)
        self.assertIsNone(self.engine.track)
        self.assertEqual(self.engine.message, "Неизвестная метка: красно-жёлтый")

    def test_labelled_disc_binds_without_conflict_and_empty_label_forgets_it(self):
        self.engine.set_label("NEW", "синий")
        self.scan_ready()
        self.engine.tick("NEW", 1.5)
        self.assertEqual(self.store.snapshot()["tags"]["NEW"], {"track": "b", "label": "синий"})
        self.assertIn("синий привязана", self.engine.message)
        self.engine.unbind("NEW")
        self.engine.set_label("NEW", "")
        self.assertNotIn("NEW", self.store.snapshot()["tags"])
        with self.assertRaises(ValueError):
            self.engine.set_label("NEW", "x" * 81)

    def test_rename(self):
        self.engine.tick("TAG_A", 1)
        self.engine.rename("a", "  Новое   имя ")
        self.assertEqual(self.store.snapshot()["tracks"]["a"]["title"], "Новое имя")
        self.assertEqual(self.engine.message, "Воспроизведение: Новое имя")
        for bad in ("", "   ", "x" * 241):
            with self.assertRaises(ValueError):
                self.engine.rename("a", bad)
        with self.assertRaises(ValueError):
            self.engine.rename("nope", "x")

    def test_delete_track_stops_it_unbinds_and_removes_file(self):
        self.engine.set_label("TAG_A", "диск")
        self.engine.tick("TAG_A", 1)
        self.engine.delete_track("a")
        self.assertIsNone(self.engine.track)
        self.assertEqual(self.player.events[-1], "stop")
        self.assertFalse((self.store.root / "music" / "a.mp3").exists())
        self.assertNotIn("a", self.store.snapshot()["tracks"])
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"], {"track": None, "label": "диск"})
        self.engine.tick("TAG_A", 1.2)  # the present tag does not restart anything
        self.assertEqual(self.player.events.count("a.mp3"), 1)
        self.engine.delete_track("b")
        self.assertNotIn("TAG_B", self.store.snapshot()["tags"])
        with self.assertRaises(ValueError):
            self.engine.delete_track("a")

    def test_gain_and_trim_reach_the_player_and_positions_are_relative(self):
        self.engine.tick("TAG_A", 1)
        self.assertEqual(self.player.last, {"start": 0.0, "end": None, "gain_db": 0.0})
        self.store.set_analysis("a", loudness_lufs=-20.0, true_peak_db=-0.5, duration=100.0)
        self.engine.set_sound("a", gain_offset_db=1.5, trim_start=10.0, trim_end=90.0)
        self.engine.manual_play("a")
        # target -16: +4 dB wanted, but the true peak caps the auto gain at -0.5 dB; plus the +1.5 offset
        self.assertEqual(self.player.last, {"start": 10.0, "end": 90.0, "gain_db": 1.0})
        self.assertEqual(self.engine.status()["tracks"]["a"]["gain_db"], -0.5)
        self.player.position, self.player.duration = 25.0, 100.0
        status = self.engine.status()
        self.assertEqual((status["position"], status["duration"]), (15.0, 80.0))
        self.engine.seek(30)
        self.assertEqual(self.player.events[-1], ("seek", 40.0))
        self.engine.seek(500)
        self.assertEqual(self.player.events[-1], ("seek", 89.9))
        self.store.update_settings(normalize=False)
        self.engine.manual_play("a")
        self.assertEqual(self.player.last["gain_db"], 1.5)
        self.engine.manual_play("a", position=5)
        self.assertEqual(self.player.last["start"], 15.0)  # inside the load, not a seek: mpv is not ready yet
        self.engine.manual_play("a", position=500)
        self.assertEqual(self.player.last["start"], 89.9)

    def test_sound_validation(self):
        self.store.set_analysis("a", -12.0, -1.0, 60.0)
        for bad in (dict(gain_offset_db=25), dict(trim_start=59.8), dict(trim_start=10, trim_end=10.2)):
            with self.assertRaises(ValueError):
                self.engine.set_sound("a", **bad)
        self.engine.set_sound("a", trim_end=59.99)  # trimming nothing at the end = no trim
        self.assertIsNone(self.store.snapshot()["tracks"]["a"]["trim_end"])
        self.engine.set_sound("a", trim_start=0, trim_end=30)
        self.assertEqual(self.store.snapshot()["tracks"]["a"]["trim_start"], None)
        self.engine.set_sound("a", clear_trim=True)
        self.assertEqual(self.store.snapshot()["tracks"]["a"]["trim_end"], None)
        self.assertEqual(auto_gain({"loudness_lufs": -30.0, "true_peak_db": -20.0}, {"normalize": True, "target_lufs": -16}), 14.0)
        self.assertEqual(auto_gain({"loudness_lufs": None}, {"normalize": True, "target_lufs": -16}), 0.0)

    def test_resume_respects_trim(self):
        self.store.update_settings(continue_playback=True)
        self.engine.set_sound("a", trim_start=20.0, trim_end=80.0)
        self.engine.tick("TAG_A", 1)
        self.player.elapsed = 50.0
        self.engine.tick(None, 3)
        self.engine.tick("TAG_A", 4)
        self.assertEqual(self.player.last["start"], 50.0)
        self.player.elapsed = 79.5  # saved position past the trimmed end: start over
        self.engine.tick(None, 6)
        self.engine.tick("TAG_A", 7)
        self.assertEqual(self.player.last["start"], 20.0)

    def test_ready_sound_plays_once_and_its_eof_is_silent(self):
        self.engine.mark_ready()
        self.engine.mark_ready()
        self.assertEqual(self.player.events, ["ready-default.wav"])
        self.assertTrue(self.engine.status()["ready"])
        self.player.ended = True
        self.engine.tick(None, 1)
        self.assertEqual(self.engine.message, "Поднеси диск к считывателю")  # not "track finished"
        self.engine.tick("TAG_A", 2)  # a disc during the chime wins
        self.assertEqual(self.engine.track, "a")
        with self.assertRaises(ValueError):
            self.engine.play_ready_sound()

    def test_ready_sound_can_be_disabled_and_customized(self):
        self.store.update_settings(ready_sound=False)
        self.engine.mark_ready()
        self.assertEqual(self.player.events, [])
        custom = self.store.root / "sounds" / "x.part"
        custom.write_bytes(b"ogg")
        self.engine.sounds.set_custom(custom, ".OGG")
        self.engine.play_ready_sound()
        self.assertEqual(self.player.events[-1], "ready.ogg")
        self.engine.sounds.clear_custom()
        self.assertEqual(self.engine.sounds.current().name, "ready-default.wav")

    def test_delete_track_cancels_its_binding_and_resume(self):
        self.store.update_settings(continue_playback=True)
        self.engine.tick("TAG_A", 1)
        self.player.elapsed = 42
        self.engine.tick(None, 3)
        self.assertEqual(self.engine._resume_positions, {"TAG_A": ("a", 42)})
        self.engine.arm("a")
        self.engine.delete_track("a")
        self.assertIsNone(self.engine.pending)
        self.assertEqual(self.engine._resume_positions, {})


class AnalysisTests(unittest.TestCase):
    LOG = """[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 0.35 | silence_duration: 0.35
[silencedetect @ 0x1] silence_start: 86.75
[Parsed_ebur128_0 @ 0x2] Summary:

  Integrated loudness:
    I:         -17.1 LUFS
    Threshold: -27.9 LUFS

  Loudness range:
    LRA:         9.8 LU

  True peak:
    Peak:       -0.4 dBFS
"""

    def test_parse_log(self):
        result = parse_log(self.LOG, 89.0)
        self.assertEqual(result, {"loudness_lufs": -17.1, "true_peak_db": -0.4, "silence_start": 0.35, "silence_end": 86.75})
        silent = parse_log("I: -inf LUFS\nsilence_start: 0\n", 10.0)
        self.assertEqual((silent["loudness_lufs"], silent["silence_start"], silent["silence_end"]), (None, None, None))
        middle = parse_log("silence_start: 40\nsilence_end: 42 | silence_duration: 2\n", 89.0)
        self.assertEqual((middle["silence_start"], middle["silence_end"]), (None, None))

    def test_peaks(self):
        samples = [0] * 50 + [1000] * 50 + [-2000] * 50 + [500] * 50
        pcm = b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples)
        self.assertEqual(peaks_from_pcm(pcm, buckets=4), [0, 128, 255, 64])
        self.assertEqual(peaks_from_pcm(b"", buckets=4), [])

    def test_queue_stores_results_and_errors(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(temp.name)
        self.addCleanup(store.db.close)
        for name in ("a", "b", "bad"):
            (store.root / "music" / (name + ".mp3")).write_bytes(b"x")
            store.add_track(name, name, name + ".mp3", name)
        seen = []

        def fake_analyze(path, on_process=None):
            seen.append(path.name)
            if "bad" in path.name:
                raise RuntimeError("ffmpeg завершился с кодом 1")
            if "b" == path.stem and not seen.count("b.mp3") > 1:
                raise Interrupted("ffmpeg interrupted (code -15)")  # first attempt: killed, retried at the back
            return {"loudness_lufs": -20.0, "true_peak_db": -1.5, "duration": 30.0, "peaks": [0, 255],
                    "silence_start": 0.2, "silence_end": None}

        analyzer = Analyzer(store, analyze=fake_analyze)
        analyzer.enqueue_missing()
        analyzer.enqueue(["a"])  # already queued: no duplicate
        self.assertEqual(analyzer.status(), {"queued": 3, "running": None, "queue": ["a", "b", "bad"]})
        analyzer.discard("b")
        analyzer.run_pending()
        self.assertEqual(seen, ["a.mp3", "bad.mp3"])
        track = store.snapshot()["tracks"]["a"]
        self.assertEqual((track["loudness_lufs"], track["true_peak_db"], track["duration"]), (-20.0, -1.5, 30.0))
        self.assertIsNotNone(track["analyzed_at"])
        self.assertEqual(json.loads(store.analysis_path("a").read_text())["peaks"], [0, 255])
        self.assertIn("ffmpeg", store.snapshot()["tracks"]["bad"]["analysis_error"])
        analyzer.enqueue_missing()  # "b" is still missing and the failed "bad" gets another try
        self.assertEqual(analyzer.status(), {"queued": 2, "running": None, "queue": ["b", "bad"]})
        analyzer.enqueue_all()
        self.assertEqual(analyzer.status()["queued"], 3)
        analyzer.run_pending()
        self.assertEqual(seen.count("b.mp3"), 2)
        self.assertIsNone(store.snapshot()["tracks"]["b"]["analysis_error"])
        self.assertIsNotNone(store.snapshot()["tracks"]["b"]["analyzed_at"])
        analyzer.stopping = True  # a kill during shutdown must not be recorded as the file's fault
        store.set_analysis_error("bad", "")
        analyzer.enqueue(["bad"])
        analyzer.run_pending()
        self.assertEqual(store.snapshot()["tracks"]["bad"]["analysis_error"], "")
        store.delete_track("a")
        self.assertFalse(store.analysis_path("a").exists())


class ChimeTests(unittest.TestCase):
    def test_builtin_chime_is_a_short_mono_wav(self):
        import wave
        with wave.open(io.BytesIO(chime_wav())) as data:
            self.assertEqual((data.getnchannels(), data.getsampwidth(), data.getframerate()), (1, 2, 44100))
            self.assertAlmostEqual(data.getnframes() / data.getframerate(), 0.75, places=2)
        with tempfile.TemporaryDirectory() as root:
            sounds = ReadySounds(root)
            self.assertTrue(sounds.default.is_file())
            self.assertIsNone(sounds.custom())


class StoreImportTests(unittest.TestCase):
    def test_library_json_is_imported_once(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = {"version": 1,
                      "tracks": {"t1": {"title": "Song", "file": "t1.mp3", "original": "Song.mp3"}},
                      "tags": {"AA": "t1", "BB": "gone"},
                      "settings": {"volume": 70, "remove_delay": 1.2, "continue_playback": True}}
            Path(root, "library.json").write_text(json.dumps(legacy), encoding="utf-8")
            store = Store(root)
            data = store.snapshot()
            self.assertEqual(data["tracks"]["t1"]["title"], "Song")
            self.assertEqual(data["tags"], {"AA": {"track": "t1", "label": None}})  # BB pointed at a missing track
            self.assertEqual(data["settings"], {"volume": 70, "read_grace_ms": 1000, "continue_playback": True,
                                                "normalize": True, "target_lufs": -16.0,
                                                "ready_sound": True, "ready_sound_file": None})
            store.db.close()
            Path(root, "library.json").write_text(json.dumps({**legacy, "tracks": {}}), encoding="utf-8")
            again = Store(root)  # the DB is not empty: no second import
            self.assertEqual(again.snapshot()["tracks"].keys(), {"t1"})
            again.db.close()


class FakeChip:
    MI_OK = 0
    MI_ERR = 2
    PICC_REQALL = 0x52
    PCD_TRANSCEIVE = 0x0C
    PCD_IDLE = 0x00
    CommandReg, FIFODataReg, FIFOLevelReg, BitFramingReg = 0x01, 0x09, 0x0A, 0x0D

    def __init__(self, uid, corrupt=False, version=0x82):
        self.uid = bytes.fromhex(uid)
        self.corrupt, self.version = corrupt, version
        self.present = True
        self.requests = 0
        self.fifo = []
        self.commands = []

    def Read_MFRC522(self, register):
        return self.version

    def Write_MFRC522(self, register, value):
        if register == self.FIFODataReg:
            self.fifo.append(value)
        elif register == self.CommandReg:
            self.commands.append(value)

    def SetBitMask(self, register, mask):
        if register == self.FIFOLevelReg and mask & 0x80:
            self.fifo.clear()

    def MFRC522_Request(self, command):
        self.requests += 1
        return (self.MI_OK, 16) if self.present else (self.MI_ERR, 0)

    def MFRC522_Anticoll(self):
        if self.corrupt:
            return self.MI_ERR, []  # real library returns MI_ERR on bad BCC
        remaining = list(self.uid)
        cascade = len(remaining) > 4
        block = [0x88] + remaining[:3] if cascade else remaining[:4]
        bcc = block[0] ^ block[1] ^ block[2] ^ block[3]
        return self.MI_OK, block + [bcc]

    def MFRC522_ToCard(self, command, data):
        return self.MI_ERR, [], 0  # HALT is never acknowledged by a tag

    def Close_MFRC522(self):
        pass


class ReaderTests(unittest.TestCase):
    def test_uids(self):
        # 4-byte UID: returned as-is
        self.assertEqual(Reader(FakeChip("DEADBEEF")).read(), "DEADBEEF")
        # 7-byte cascade UID: first byte is 0x88 (cascade marker), next 3 are UID bytes
        self.assertEqual(Reader(FakeChip("04AABB11223344")).read(), "8804AABB")
        # Reading twice returns the same value (tag stays readable)
        r = Reader(FakeChip("DEADBEEF"))
        self.assertEqual(r.read(), r.read())

    def test_corrupt_bcc_does_not_bind(self):
        self.assertIsNone(Reader(FakeChip("04AABB11223344", corrupt=True)).read())

    def test_halt_is_transmitted_without_waiting_for_a_reply(self):
        chip = FakeChip("DEADBEEF")
        Reader(chip).read()
        self.assertEqual(chip.fifo, [0x50, 0x00, 0x57, 0xCD])
        self.assertEqual(chip.commands[-2:], [0x04, chip.PCD_IDLE])  # Transmit, then Idle

    def test_retries_only_while_a_tag_was_present(self):
        chip = FakeChip("DEADBEEF")
        reader = Reader(chip)
        reader.read()
        chip.present, chip.requests = False, 0
        self.assertIsNone(reader.read())  # tag just vanished: retried
        self.assertEqual(chip.requests, 3)
        chip.requests = 0
        self.assertIsNone(reader.read())  # still empty: single attempt
        self.assertEqual(chip.requests, 1)

    def test_disconnected_reader(self):
        with self.assertRaises(RuntimeError):
            Reader(FakeChip("DEADBEEF", version=0))

    def test_crc_known_halt_frame(self):
        self.assertEqual(crc_a([0x50, 0]), [0x57, 0xCD])


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs Unix sockets")
class PlayerTests(unittest.TestCase):
    def setUp(self):
        fake = Path(__file__).with_name("fake_mpv.py")
        self.player = Player(volume=50, mpv=[sys.executable, str(fake)])
        self.addCleanup(self.player.close)

    def poll_until(self, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = self.player.poll()
            if result:
                return result
            time.sleep(0.02)
        return None

    def test_eof_reported_once(self):
        self.player.play("track-eof.mp3")
        self.assertEqual(self.poll_until(), "eof")
        self.assertIsNone(self.poll_until(0.3))

    def test_eof_of_replaced_track_is_ignored(self):
        self.player.play("old-eof.mp3")
        self.player.play("new.mp3")
        self.assertIsNone(self.poll_until(0.5))

    def test_load_error_raises(self):
        self.player.play("broken.mp3")
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            self.poll_until()

    def test_crash_while_playing_raises_and_next_play_restarts(self):
        self.player.play("crash.mp3")
        with self.assertRaisesRegex(RuntimeError, "mpv"):
            self.poll_until()
        self.player.play("next-eof.mp3")
        self.assertEqual(self.poll_until(), "eof")

    def test_stop_keeps_elapsed_and_idle_has_no_position(self):
        self.player.play("song.mp3")
        self.assertEqual(self.player.position, 12.5)
        self.assertEqual(self.player.duration, 60.0)
        self.player.stop()
        self.assertEqual(self.player.elapsed, 12.5)
        self.assertIsNone(self.player.position)
        with self.assertRaises(ValueError):
            self.player.seek(3)


class HttpTests(unittest.TestCase):
    setUp = EngineTests.setUp
    open_store = EngineTests.open_store

    def client(self):
        return TestClient(create_app(self.engine, static_dir=Path(self.temp.name) / "no-static"))

    def test_status_and_manual_controls(self):
        client = self.client()
        self.assertIn("tracks", client.get("/api/status").json())
        self.assertTrue(client.post("/api/play", json={"track": "a"}, headers=JUKEBOX).json()["ok"])
        self.assertEqual(self.engine.track, "a")
        self.assertTrue(client.post("/api/stop", headers=JUKEBOX).json()["ok"])
        self.assertIsNone(self.engine.track)

    def test_post_needs_jukebox_header(self):
        response = self.client().post("/api/stop")
        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.json())

    def test_errors_are_json_with_message(self):
        client = self.client()
        missing = client.post("/api/play", json={"track": "nope"}, headers=JUKEBOX)
        self.assertEqual((missing.status_code, missing.json()["error"]), (400, "Трек не найден"))
        invalid = client.post("/api/settings", json={"volume": 500, "read_grace_ms": 10}, headers=JUKEBOX)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("volume", invalid.json()["error"])

    def test_settings_persist(self):
        body = {"volume": 60, "read_grace_ms": 1500, "continue_playback": True}
        self.client().post("/api/settings", json=body, headers=JUKEBOX)
        saved = self.store.snapshot()["settings"]
        self.assertEqual({k: saved[k] for k in body}, body)
        self.assertEqual(saved["target_lufs"], -16.0)  # fields an older client leaves out keep their defaults
        self.assertIn(60, self.player.events)

    def test_upload_duplicate_and_path_safety(self):
        client = self.client()
        with patch("server.subprocess.run") as probe:
            probe.return_value.stdout = '{"streams":[{"codec_name":"mp3"}]}'
            one = client.post("/api/upload", params={"name": "../../test.mp3"}, content=b"music", headers=JUKEBOX).json()
            two = client.post("/api/upload", params={"name": "different.mp3"}, content=b"music", headers=JUKEBOX).json()
        self.assertEqual(one["id"], two["id"])
        self.assertTrue(two["duplicate"])
        track = self.store.snapshot()["tracks"][one["id"]]
        self.assertEqual(track["title"], "test")
        self.assertTrue((self.store.root / "music" / track["file"]).exists())
        self.assertEqual(list((self.store.root / "music").glob("*.part")), [])

    def test_upload_rejects_unknown_extension(self):
        response = self.client().post("/api/upload", params={"name": "x.exe"}, content=b"x", headers=JUKEBOX)
        self.assertEqual(response.status_code, 400)

    def test_tags_are_objects_and_labels_are_editable(self):
        client = self.client()
        self.assertEqual(client.get("/api/status").json()["tags"]["TAG_A"], {"track": "a", "label": None})
        self.assertEqual(client.put("/api/tags/TAG_A", json={"label": "диск"}, headers=JUKEBOX).status_code, 200)
        self.assertEqual(client.get("/api/status").json()["tags"]["TAG_A"], {"track": "a", "label": "диск"})
        self.assertEqual(client.put("/api/tags/TAG_A", json={"label": "диск"}).status_code, 403)
        self.assertEqual(client.put("/api/tags/TAG_A", json={"label": "x" * 81}, headers=JUKEBOX).status_code, 400)

    def test_sound_update_analyze_and_waveform(self):
        analyzer = Analyzer(self.store, analyze=lambda path, on_process=None: {
            "loudness_lufs": -10.0, "true_peak_db": -3.0, "duration": 120.0, "peaks": [1, 2, 3],
            "silence_start": None, "silence_end": 118.0})
        self.engine.analyzer = analyzer
        client = self.client()
        self.assertEqual(client.get("/api/status").json()["analysis"], {"queued": 0, "running": None, "queue": []})
        self.assertEqual(client.get("/api/tracks/a/waveform").status_code, 404)
        self.assertEqual(client.post("/api/analyze", json={}, headers=JUKEBOX).status_code, 200)
        self.assertEqual(client.get("/api/status").json()["analysis"]["queued"], 2)
        analyzer.run_pending()
        wave = client.get("/api/tracks/a/waveform").json()
        self.assertEqual((wave["peaks"], wave["silence_end"]), ([1, 2, 3], 118.0))
        track = client.get("/api/status").json()["tracks"]["a"]
        self.assertEqual((track["loudness_lufs"], track["gain_db"]), (-10.0, -6.0))  # -16 target; peak cap (+2) not reached
        self.assertEqual(client.put("/api/tracks/a", json={"gain_offset_db": -2.5, "trim_start": 1.5, "trim_end": 118.0},
                                    headers=JUKEBOX).status_code, 200)
        track = client.get("/api/status").json()["tracks"]["a"]
        self.assertEqual((track["gain_offset_db"], track["trim_start"], track["trim_end"]), (-2.5, 1.5, 118.0))
        self.assertEqual(client.put("/api/tracks/a", json={"trim_start": None, "trim_end": None}, headers=JUKEBOX).status_code, 200)
        self.assertIsNone(client.get("/api/status").json()["tracks"]["a"]["trim_start"])
        self.assertEqual(client.put("/api/tracks/a", json={"gain_offset_db": 99}, headers=JUKEBOX).status_code, 400)
        self.assertEqual(client.post("/api/analyze", json={"track": "nope"}, headers=JUKEBOX).status_code, 400)
        self.assertEqual(client.post("/api/play", json={"track": "a", "position": 7}, headers=JUKEBOX).status_code, 200)
        self.assertEqual(self.player.last["start"], 7.0)  # trim was cleared above
        body = {"volume": 60, "read_grace_ms": 1500, "continue_playback": True, "normalize": False, "target_lufs": -14}
        client.post("/api/settings", json=body, headers=JUKEBOX)
        self.assertEqual(self.store.snapshot()["settings"]["target_lufs"], -14)

    def test_ready_sound_routes(self):
        client = self.client()
        self.assertEqual(client.post("/api/ready-sound/play", headers=JUKEBOX).status_code, 200)
        self.assertEqual(self.player.events[-1], "ready-default.wav")
        with patch("server.subprocess.run") as probe:
            probe.return_value.stdout = '{"streams":[{"codec_name":"mp3"}]}'
            response = client.post("/api/ready-sound", params={"name": "ding.mp3"}, content=b"music", headers=JUKEBOX)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/api/status").json()["settings"]["ready_sound_file"], "ding.mp3")
        self.assertEqual(self.engine.sounds.current().name, "ready.mp3")
        self.assertEqual(client.delete("/api/ready-sound", headers=JUKEBOX).status_code, 200)
        self.assertIsNone(client.get("/api/status").json()["settings"]["ready_sound_file"])
        self.assertEqual(self.engine.sounds.current().name, "ready-default.wav")
        self.assertEqual(list((self.store.root / "sounds").glob("*.part")), [])
        self.assertEqual(client.post("/api/ready-sound", params={"name": "x.txt"}, content=b"x", headers=JUKEBOX).status_code, 400)

    def test_rename_and_delete_track(self):
        client = self.client()
        self.assertEqual(client.put("/api/tracks/a", json={"title": "Песня"}, headers=JUKEBOX).status_code, 200)
        self.assertEqual(client.get("/api/status").json()["tracks"]["a"]["title"], "Песня")
        self.assertEqual(client.put("/api/tracks/a", json={"title": " "}, headers=JUKEBOX).status_code, 400)
        self.assertEqual(client.delete("/api/tracks/a").status_code, 403)
        self.assertEqual(client.delete("/api/tracks/a", headers=JUKEBOX).status_code, 200)
        status = client.get("/api/status").json()
        self.assertNotIn("a", status["tracks"])
        self.assertNotIn("TAG_A", status["tags"])
        self.assertEqual(client.delete("/api/tracks/a", headers=JUKEBOX).status_code, 400)

    def test_root_without_frontend_build_is_a_clear_error(self):
        response = self.client().get("/")
        self.assertEqual(response.status_code, 503)
        self.assertIn("static", response.json()["error"])

    def test_status_key_ignores_position(self):
        status = self.engine.status()
        self.assertEqual(status_key({**status, "position": 1.0}), status_key({**status, "position": 2.0}))
        self.assertNotEqual(status_key(status), status_key({**status, "message": "other"}))

if __name__ == "__main__":
    unittest.main()
