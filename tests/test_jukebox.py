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

from core import Engine, Store
from player import Player
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

    def play(self, path, offset=0.0):
        self.events.append(Path(path).name)

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
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"], "a")

    def test_conflict_needs_exact_confirmation_and_persists(self):
        self.scan_ready()
        self.engine.tick("TAG_A", 1.5)
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"], "a")
        with self.assertRaises(ValueError):
            self.engine.confirm("TAG_B", "b")
        self.engine.confirm("TAG_A", "b")
        self.assertEqual(self.open_store().snapshot()["tags"]["TAG_A"], "b")

    def test_new_binding_and_cancel_do_not_autoplay(self):
        self.scan_ready()
        self.now = 1.5
        self.engine.tick("NEW", 1.5)
        self.assertEqual(self.store.snapshot()["tags"]["NEW"], "b")
        self.engine.tick("NEW", 1.6)
        self.assertNotIn("b.mp3", self.player.events)

    def test_binding_timeout(self):
        self.scan_ready()
        self.engine.tick("TAG_A", 1.5)
        self.now = 121
        with self.assertRaises(ValueError):
            self.engine.confirm("TAG_A", "b")
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"], "a")

    def open_store(self):
        store = Store(self.temp.name)
        self.addCleanup(store.db.close)
        return store

    def test_failed_write_rolls_back(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.bind_tag("TAG_A", "no-such-track")  # foreign key violation
        self.assertEqual(self.store.snapshot()["tags"]["TAG_A"], "a")
        self.assertEqual(self.open_store().snapshot()["tags"]["TAG_A"], "a")
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
                         {"volume": 35, "read_grace_ms": 1000, "continue_playback": False})
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
            self.assertEqual(data["tags"], {"AA": "t1"})  # BB pointed at a missing track
            self.assertEqual(data["settings"], {"volume": 70, "read_grace_ms": 1000, "continue_playback": True})
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
        self.assertEqual(self.store.snapshot()["settings"], body)
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

    def test_legacy_ui_served_without_frontend_build(self):
        response = self.client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_status_key_ignores_position(self):
        status = self.engine.status()
        self.assertEqual(status_key({**status, "position": 1.0}), status_key({**status, "position": 2.0}))
        self.assertNotEqual(status_key(status), status_key({**status, "message": "other"}))

if __name__ == "__main__":
    unittest.main()
