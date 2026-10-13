import os
import threading
import time
from pathlib import Path
import logging
import subprocess
import tempfile

LOG = logging.getLogger('jukebox.audio')

_WAV_HEADER = 44
_BYTES_PER_SEC = 48000 * 2 * 2  # 48 kHz, stereo, 16-bit = 192000


class Player:
    def __init__(self, volume=35, device='plughw:CARD=MAX98357A,DEV=0'):
        self.level = volume
        self.device = device
        self.process = None
        self.output = None
        self._feeder_thread = None
        self._play_start = None
        self._offset = 0.0
        self.elapsed = 0.0
        self._prepared_path = None

    def prepare(self, path):
        path = Path(path)
        cache = path.parent.parent / "pcm-cache"
        cache.mkdir(exist_ok=True)
        stat = path.stat()
        target = cache / f"{path.stem}-{stat.st_size}-{stat.st_mtime_ns}-v{self.level}-right.wav"
        if target.exists():
            return target
        fd, name = tempfile.mkstemp(suffix=".wav", dir=cache)
        os.close(fd)
        temp = Path(name)
        try:
            LOG.info("PREPARE %s", path)
            result = subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(path),
                "-vn", "-af", f"aformat=channel_layouts=stereo,pan=stereo|c0=0*c0|c1=c1,volume={self.level / 100}",
                "-ar", "48000", "-c:a", "pcm_s16le", "-bitexact", "-map_metadata", "-1", str(temp)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
            if result.returncode:
                raise RuntimeError("ffmpeg: " + result.stderr.decode(errors="replace")[-3000:])
            os.replace(temp, target)
            return target
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _feed_wav(wav_path, audio_skip, write_fd):
        try:
            with open(wav_path, 'rb') as f:
                header = bytearray(f.read(_WAV_HEADER))
                orig_data = int.from_bytes(header[40:44], 'little')
                new_data = max(0, orig_data - audio_skip)
                header[40:44] = new_data.to_bytes(4, 'little')
                header[4:8] = (36 + new_data).to_bytes(4, 'little')
                os.write(write_fd, bytes(header))
                f.seek(_WAV_HEADER + audio_skip)
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    os.write(write_fd, chunk)
        except OSError:
            pass
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass

    @property
    def position(self):
        if self._play_start is None:
            return None
        return self._offset + (time.monotonic() - self._play_start)

    @property
    def duration(self):
        if self._prepared_path is None:
            return None
        try:
            size = self._prepared_path.stat().st_size
            audio_bytes = max(0, size - _WAV_HEADER)
            return audio_bytes / _BYTES_PER_SEC
        except OSError:
            return None

    def play(self, path, offset=0.0):
        self.stop()
        prepared = self.prepare(path)
        self._prepared_path = prepared
        self._offset = offset
        self._play_start = time.monotonic()
        self.output = tempfile.TemporaryFile()
        if offset > 0.0:
            audio_skip = (int(offset * _BYTES_PER_SEC) // 4) * 4
            file_size = prepared.stat().st_size
            audio_skip = min(audio_skip, max(0, file_size - _WAV_HEADER))
            read_fd, write_fd = os.pipe()
            self._feeder_thread = threading.Thread(
                target=self._feed_wav, args=(prepared, audio_skip, write_fd), daemon=True
            )
            self._feeder_thread.start()
            args = ['aplay', '-D', self.device, '--buffer-size=48000', '--period-size=12000', '-']
            LOG.info("PLAY %s device=%s volume=%s offset=%.1f", prepared, self.device, self.level, offset)
            try:
                self.process = subprocess.Popen(args, stdin=read_fd, stdout=self.output, stderr=subprocess.STDOUT)
                os.close(read_fd)
            except Exception:
                os.close(read_fd)
                self.output.close()
                self.output = None
                raise
        else:
            self._feeder_thread = None
            args = ["aplay", "-D", self.device, "--buffer-size=48000", "--period-size=12000", str(prepared)]
            LOG.info("PLAY %s device=%s volume=%s", prepared, self.device, self.level)
            try:
                self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.STDOUT)
            except Exception:
                self.output.close()
                self.output = None
                raise

    def poll(self):
        if self.process is None:
            return None
        code = self.process.poll()
        if code is None:
            return None
        self._play_start = None
        self._feeder_thread = None
        self.output.seek(0)
        detail = self.output.read()[-12000:].decode('utf-8', errors='replace')
        self.output.close()
        self.output = None
        self.process = None
        if code != 0:
            LOG.error('APLAY exit=%s: %s', code, detail)
            raise RuntimeError(f'aplay завершился с кодом {code}: {detail[-3000:]}')
        LOG.info('EOF')
        return 'eof'

    def stop(self):
        if self._play_start is not None:
            self.elapsed = self._offset + (time.monotonic() - self._play_start)
            self._play_start = None
        self._prepared_path = None
        if self.process is not None:
            if self.process.poll() is None:
                LOG.info('STOP')
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
        self._feeder_thread = None
        if self.output:
            self.output.close()
            self.output = None

    def volume(self, value):
        self.level = value

    def close(self):
        self.stop()
