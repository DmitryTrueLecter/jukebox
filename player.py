"""Audio playback through one long-lived mpv process controlled over JSON IPC.

mpv decodes on the fly and writes to the ALSA device "jukebox" (deploy/asound.conf),
which mixes stereo to mono on both channels for the MAX98357A.
"""
import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

LOG = logging.getLogger("jukebox.audio")


class Player:
    def __init__(self, volume=35, device="alsa/jukebox", mpv="mpv", start_timeout=30.0):
        self.level = volume
        self.start_timeout = start_timeout
        self.device = device
        self.mpv = [mpv] if isinstance(mpv, str) else list(mpv)
        self.elapsed = 0.0  # position at the last stop(), used for resume
        self.process = None
        self.sock = None
        self.socket_path = Path(tempfile.gettempdir()) / f"jukebox-mpv-{os.getpid()}.sock"
        self.lock = threading.RLock()  # serializes commands
        self.cond = threading.Condition()  # guards responses/events, filled by the reader thread
        self.responses = {}
        self.events = []
        self.next_id = 0
        self.entry = None  # mpv playlist_entry_id of the file we are playing

    # --- mpv process and IPC ---------------------------------------------

    def _alive(self):
        return self.process is not None and self.process.poll() is None

    def _start(self):
        self._shutdown()
        self.socket_path.unlink(missing_ok=True)
        self.process = subprocess.Popen([
            *self.mpv, "--idle=yes", "--no-config", "--no-video", "--no-terminal",
            "--load-scripts=no", "--ytdl=no", "--ao=alsa", f"--audio-device={self.device}",
            f"--volume={self.level}", f"--input-ipc-server={self.socket_path}",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # A cold boot on the Pi 3 can take mpv well over 5 s to get going; a timeout must not leave an
        # orphaned process behind, or every later command would find it "alive" without a socket.
        deadline = time.monotonic() + self.start_timeout
        while True:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self.socket_path))
                break
            except OSError:
                sock.close()
                if self.process.poll() is not None:
                    code = self.process.returncode
                    self._shutdown()
                    raise RuntimeError(f"mpv не запустился, код {code}")
                if time.monotonic() > deadline:
                    self._shutdown()
                    raise RuntimeError(f"mpv не открыл IPC-сокет за {self.start_timeout:.0f} с")
                time.sleep(0.05)
        self.sock = sock
        with self.cond:
            self.responses.clear()
            self.events.clear()
        threading.Thread(target=self._read, args=(sock,), daemon=True).start()
        LOG.info("MPV started pid=%s device=%s", self.process.pid, self.device)

    def _read(self, sock):
        try:
            for line in sock.makefile("rb"):
                message = json.loads(line)
                with self.cond:
                    if "request_id" in message:
                        self.responses[message["request_id"]] = message
                        self.cond.notify_all()
                    elif message.get("event") == "end-file":
                        self.events.append(message)
        except (OSError, ValueError):
            pass

    def _command(self, *command, timeout=5):
        if not self._alive() or self.sock is None:
            self._start()
        with self.cond:
            self.next_id += 1
            request_id = self.next_id
        payload = json.dumps({"command": list(command), "request_id": request_id}) + "\n"
        self.sock.sendall(payload.encode())
        with self.cond:
            if not self.cond.wait_for(lambda: request_id in self.responses, timeout):
                raise RuntimeError(f"mpv не ответил на {command[0]}")
            response = self.responses.pop(request_id)
        if response.get("error") != "success":
            raise RuntimeError(f"mpv {command[0]}: {response.get('error')}")
        return response.get("data")

    def _get(self, name):
        if self.entry is None or not self._alive():
            return None
        try:
            return self._command("get_property", name, timeout=1)
        except (OSError, RuntimeError):
            return None

    def _shutdown(self):
        if self.sock is not None:
            try:
                self.sock.sendall(b'{"command": ["quit"]}\n')
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        self.socket_path.unlink(missing_ok=True)

    # --- interface used by Engine ------------------------------------------

    @property
    def position(self):
        with self.lock:
            return self._get("time-pos")

    @property
    def duration(self):
        with self.lock:
            return self._get("duration")

    def play(self, path, start=0.0, end=None, gain_db=0.0):
        """Start the file at `start` seconds, stop at `end` (None = EOF), with `gain_db` applied by an audio filter."""
        with self.lock:
            self.stop()
            options = [f"start={start:.3f}"] if start > 0 else []
            if end is not None:
                options.append(f"end={end:.3f}")
            # The filter chain is a global property, so it is set before every load (and cleared when not needed).
            self._command("set_property", "af", f"lavfi=[volume={gain_db:.2f}dB]" if abs(gain_db) >= 0.05 else "")
            result = self._command("loadfile", str(path), "replace", -1, ",".join(options))
            self.entry = result["playlist_entry_id"]
            LOG.info("PLAY %s volume=%s start=%.1f end=%s gain=%.1f", path, self.level, start, end, gain_db)

    def seek(self, position):
        with self.lock:
            if self.entry is None:
                raise ValueError("Ничего не воспроизводится")
            self._command("seek", max(0.0, position), "absolute")

    def poll(self):
        with self.lock:
            if self.process is not None and not self._alive():
                code = self.process.returncode
                playing = self.entry is not None
                self._shutdown()
                self.entry = None
                LOG.error("MPV exited code=%s", code)
                if playing:
                    raise RuntimeError(f"mpv завершился с кодом {code}")
                return None
            with self.cond:
                events, self.events = self.events, []
            for event in events:
                if self.entry is None or event.get("playlist_entry_id") != self.entry:
                    continue  # a file we already replaced or stopped
                self.entry = None
                if event.get("reason") == "eof":
                    LOG.info("EOF")
                    return "eof"
                if event.get("reason") == "error":
                    detail = event.get("file_error", "unknown error")
                    LOG.error("MPV playback error: %s", detail)
                    raise RuntimeError(f"Ошибка воспроизведения: {detail}")
            return None

    def stop(self):
        with self.lock:
            if self.entry is None:
                return
            position = self._get("time-pos")
            if position is not None:
                self.elapsed = position
            self.entry = None
            if self._alive():
                LOG.info("STOP")
                self._command("stop")

    def volume(self, value):
        with self.lock:
            self.level = value
            if self._alive():
                self._command("set_property", "volume", value)

    def close(self):
        with self.lock:
            self.entry = None
            self._shutdown()
