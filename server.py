import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from core import Engine, Store

LOG = logging.getLogger("jukebox")
MAX_UPLOAD = 200 * 1024 * 1024
EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus"}


def upload(store, stream, length, name):
    name = name.replace("\\", "/").split("/")[-1]
    extension = Path(name).suffix.lower()
    if extension not in EXTENSIONS:
        raise ValueError("Поддерживаются MP3, FLAC, WAV, OGG, M4A, AAC, OPUS")
    if not 0 < length <= MAX_UPLOAD:
        raise ValueError("Максимальный размер одного файла: 200 МБ")
    temp = store.root / "music" / (uuid.uuid4().hex + ".part")
    digest = hashlib.sha256()
    try:
        with temp.open("xb") as output:
            remaining = length
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("Загрузка оборвалась, повтори её")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        result = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-select_streams", "a:0", "-show_entries", "stream=codec_name",
            "-of", "json", str(temp)
        ], capture_output=True, text=True, timeout=30, check=True)
        if not json.loads(result.stdout).get("streams"):
            raise ValueError("В файле не найдена аудиодорожка")
        track_id = digest.hexdigest()
        with store.lock:
            existing = store.data["tracks"].get(track_id)
            if existing and (store.root / "music" / existing["file"]).is_file():
                return {"id": track_id, "duplicate": True}
            filename = track_id + extension
            os.replace(temp, store.root / "music" / filename)
            store.update(lambda d: d["tracks"].__setitem__(track_id, {
                "title": Path(name).stem[:240], "file": filename, "original": name[:255]
            }))
        return {"id": track_id, "duplicate": False}
    except subprocess.CalledProcessError as exc:
        raise ValueError("Файл не удалось распознать как аудио") from exc
    finally:
        temp.unlink(missing_ok=True)


def handler_factory(engine):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(120)

        def send(self, status, payload, content_type="application/json; charset=utf-8", cookie=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self.send(200, Path(__file__).with_name("index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self.send(200, engine.status())
            else:
                self.send(404, {"error": "Не найдено"})

        def do_POST(self):
            try:
                if self.headers.get("X-Jukebox") != "1":
                    self.send(403, {"error": "Недопустимый запрос"})
                    return
                parsed = urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                if parsed.path == "/api/upload":
                    name = parse_qs(parsed.query).get("name", [""])[0]
                    self.send(200, upload(engine.store, self.rfile, length, name))
                    return
                if not 0 <= length <= 8192:
                    raise ValueError("Недопустимый размер запроса")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Ожидается JSON-объект")
                with engine.lock:
                    if parsed.path == "/api/play":
                        engine.manual_play(str(body["track"]))
                    elif parsed.path == "/api/stop":
                        engine.manual_stop()
                    elif parsed.path == "/api/bind":
                        engine.arm(str(body["track"]))
                    elif parsed.path == "/api/confirm":
                        engine.confirm(str(body["uid"]), str(body["track"]))
                    elif parsed.path == "/api/cancel":
                        engine.cancel()
                    elif parsed.path == "/api/unbind":
                        engine.unbind(str(body["uid"]))
                    elif parsed.path == "/api/seek":
                        engine.seek(float(body["position"]))
                    elif parsed.path == "/api/settings":
                        volume = int(body["volume"])
                        delay = int(body["read_grace_ms"])
                        resume = bool(body.get("continue_playback", False))
                        if not 0 <= volume <= 100 or not 1 <= delay <= 60000:
                            raise ValueError("Громкость: 0–100. Защита: 1–60000 мс")
                        engine.player.volume(volume)
                        engine.store.update(lambda d: d["settings"].update(
                            volume=volume, read_grace_ms=delay, continue_playback=resume))
                    else:
                        self.send(404, {"error": "Не найдено"})
                        return
                self.send(200, {"ok": True})
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception as exc:
                LOG.exception("Request failed")
                self.send(500, {"error": str(exc)})

        def log_message(self, fmt, *args):
            if self.command != "GET" or self.path != "/api/status":
                LOG.info("%s %s", self.address_string(), fmt % args)
    return Handler


def worker(engine, stopping, reader=None):
    from reader import Reader
    retry_at = 0
    own_reader = reader is None
    try:
        while not stopping.is_set():
            uid = None
            try:
                if reader is None and time.monotonic() >= retry_at:
                    reader = Reader()
                if reader:
                    uid = reader.read()
                    with engine.lock:
                        engine.reader_error = None
            except Exception as exc:
                LOG.exception("Reader failure")
                with engine.lock:
                    engine.reader_error = str(exc)
                if reader and own_reader:
                    try:
                        reader.close()
                    except Exception:
                        LOG.exception("Reader cleanup failure")
                reader = None if own_reader else reader
                retry_at = time.monotonic() + 2
            try:
                engine.tick(uid)
            except Exception as exc:
                LOG.exception("Playback failure")
                with engine.lock:
                    engine.audio_error = str(exc)
                    engine.suppressed = engine.current
                    engine.stop()
                    engine.message = "Ошибка воспроизведения"
            stopping.wait(0.15)
    finally:
        if reader and own_reader:
            reader.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(Path.home() / "jukebox-data"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.data)
    from player import Player
    player = Player(store.snapshot()["settings"]["volume"])
    engine = Engine(store, player)
    stopping = threading.Event()
    try:
        from reader import Reader
        reader = Reader()
    except Exception as exc:
        LOG.warning("Reader init failed in main thread: %s", exc)
        reader = None
    thread = threading.Thread(target=worker, args=(engine, stopping, reader), daemon=True)
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(engine))
    server.daemon_threads = True
    def shutdown(*_):
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    thread.start()
    LOG.info("Web interface on port %s", args.port)
    try:
        server.serve_forever()
    finally:
        stopping.set()
        thread.join(timeout=5)
        server.server_close()
        engine.player.close()
        if reader:
            try:
                reader.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
