import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from core import Engine, Store
from reader import Reader, crc_a
from server import handler_factory, upload


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
        self.store = Store(self.temp.name)
        for name in ("a", "b"):
            (self.store.root / "music" / (name + ".mp3")).write_bytes(b"fake")
        def seed(data):
            data["tracks"] = {name: {"title": name, "file": name + ".mp3"} for name in ("a", "b")}
            data["tags"] = {"TAG_A": "a", "TAG_B": "b"}
        self.store.update(seed)
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
        self.assertEqual(Store(self.temp.name).snapshot()["tags"]["TAG_A"], "b")

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

    def test_failed_save_preserves_previous_mapping(self):
        with patch("core.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.update(lambda d: d["tags"].clear())
        self.assertIn("TAG_A", self.store.snapshot()["tags"])
        self.assertIn("TAG_A", Store(self.temp.name).snapshot()["tags"])

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

    def test_default_milliseconds_and_old_data_preserved(self):
        self.assertEqual(self.store.snapshot()["settings"]["read_grace_ms"], 1000)
        self.store.update(lambda d: d["settings"].pop("read_grace_ms"))
        restored = Store(self.temp.name)
        self.assertEqual(restored.snapshot()["settings"]["read_grace_ms"], 1000)
        self.assertEqual(restored.snapshot()["tags"]["TAG_A"], "a")

    def test_player_error_visible_and_not_retried_forever(self):
        self.engine.tick("TAG_A", 1)
        with patch.object(self.player, "poll", side_effect=RuntimeError("ALSA unavailable")):
            self.engine.tick("TAG_A", 1.1)
        self.engine.tick("TAG_A", 1.2)
        self.assertIsNone(self.engine.status()["playing_track"])
        self.assertEqual(self.engine.status()["audio_error"], "ALSA unavailable")
        self.assertEqual(self.player.events.count("a.mp3"), 1)

    def test_ui_stop_survives_brief_tag_absence(self):
        self.engine.tick("TAG_A", 1)
        self.engine.manual_stop()
        self.engine.tick(None, 1.4)
        self.engine.tick("TAG_A", 1.6)
        self.assertIsNone(self.engine.track)



class FakeChip:
    MI_OK = 0
    MI_ERR = 2
    PICC_REQALL = 0x52
    PCD_TRANSCEIVE = 0x0C

    def __init__(self, uid, corrupt=False, version=0x82):
        self.uid = bytes.fromhex(uid)
        self.corrupt, self.version = corrupt, version

    def Read_MFRC522(self, register):
        return self.version

    def Write_MFRC522(self, register, value):
        pass

    def MFRC522_Request(self, command):
        return self.MI_OK, 16

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

    def test_disconnected_reader(self):
        with self.assertRaises(RuntimeError):
            Reader(FakeChip("DEADBEEF", version=0))

    def test_crc_known_halt_frame(self):
        self.assertEqual(crc_a([0x50, 0]), [0x57, 0xCD])


class HttpTests(unittest.TestCase):
    setUp = EngineTests.setUp

    def test_no_auth_and_manual_controls(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.engine))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/status") as response:
                self.assertIn("tracks", json.load(response))
            for route, payload in [("play", {"track": "a"}), ("stop", {})]:
                req = urllib.request.Request(base + "/api/" + route,
                    data=json.dumps(payload).encode(), headers={"X-Jukebox": "1"})
                with urllib.request.urlopen(req) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertEqual(self.engine.track, "a" if route == "play" else None)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_upload_duplicate_and_path_safety(self):
        with patch("server.subprocess.run") as probe:
            probe.return_value.stdout = '{"streams":[{"codec_name":"mp3"}]}'
            one = upload(self.store, io.BytesIO(b"music"), 5, "../../test.mp3")
            two = upload(self.store, io.BytesIO(b"music"), 5, "different.mp3")
        self.assertEqual(one["id"], two["id"])
        self.assertTrue(two["duplicate"])
        track = self.store.snapshot()["tracks"][one["id"]]
        self.assertEqual(track["title"], "test")
        self.assertTrue((self.store.root / "music" / track["file"]).exists())


if __name__ == "__main__":
    unittest.main()
