"""The "ready" chime: played once when the service can read discs.

The built-in chime is synthesized here (two short sine notes) so the repo ships no binary.
A custom file replaces it: <data>/sounds/ready.<ext>, original name kept in settings.
"""
import io
import math
import struct
import wave
from pathlib import Path

RATE = 44100
NOTES = ((880.0, 0.00, 0.40), (1318.5, 0.14, 0.55))  # Hz, start s, length s: A5 then E6
LENGTH = 0.75


def chime_wav():
    """Mono 16-bit WAV bytes, ~0.75 s, peak at -6 dBFS."""
    samples = [0.0] * int(RATE * LENGTH)
    for freq, start, length in NOTES:
        first = int(start * RATE)
        for i in range(int(length * RATE)):
            t = i / RATE
            envelope = math.exp(-t * 6) * min(1.0, t * 300)  # fast attack, exponential decay
            samples[first + i] += envelope * (math.sin(2 * math.pi * freq * t)
                                              + 0.25 * math.sin(2 * math.pi * 2 * freq * t))
    scale = 0.5 / max(abs(s) for s in samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"".join(struct.pack("<h", int(s * scale * 32767)) for s in samples))
    return buffer.getvalue()


class ReadySounds:
    def __init__(self, root):
        self.dir = Path(root) / "sounds"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.default = self.dir / "ready-default.wav"
        if not self.default.is_file():
            self.default.write_bytes(chime_wav())

    def custom(self):
        files = sorted(p for p in self.dir.glob("ready.*") if p.is_file())
        return files[0] if files else None

    def current(self):
        return self.custom() or self.default

    def set_custom(self, source, suffix):
        self.clear_custom()
        target = self.dir / ("ready" + suffix.lower())
        source.replace(target)
        return target

    def clear_custom(self):
        for path in self.dir.glob("ready.*"):
            path.unlink(missing_ok=True)
