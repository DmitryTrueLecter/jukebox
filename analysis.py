"""Background track analysis: loudness (EBU R128), true peak, waveform peaks, leading/trailing silence.

One ffmpeg pass per track at nice 19 and a single thread, so playback never competes for the CPU.
Results: loudness and peak go to the DB (Store.set_analysis), the waveform and silence suggestions
to <data>/analysis/<track id>.json. The queue runs on upload and, on startup, for every track that
has not been analyzed yet; the UI can also ask for a re-run (one track or the whole library).
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
from array import array
from collections import deque

LOG = logging.getLogger("jukebox.analysis")

SAMPLE_RATE = 4000        # enough for a waveform picture; keeps the PCM stream small
PEAK_BUCKETS = 1000
SILENCE_FILTER = "silencedetect=n=-45dB:d=0.2"
TIMEOUT = 20 * 60         # a 200 MB FLAC on a Pi 3 at nice 19 still fits

_LOUDNESS = re.compile(r"^\s*I:\s+(-?[\d.]+|-inf) LUFS", re.M)
_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf) dBFS")
_SILENCE = re.compile(r"silence_(start|end): (-?[\d.]+)")


def _number(match):
    if match is None or match.group(1) == "-inf":
        return None
    return float(match.group(1))


def parse_log(log, duration):
    """ffmpeg's stderr → loudness, true peak and the silence to trim at both ends."""
    starts, ends = [], []
    for kind, value in _SILENCE.findall(log):
        (starts if kind == "start" else ends).append(float(value))
    silence_start = silence_end = None
    if starts and starts[0] <= 0.1:
        silence_start = ends[0] if ends else None  # no end: the whole file is silent
    if starts and (len(ends) < len(starts) or ends[-1] >= duration - 0.1):
        if starts[-1] > (silence_start or 0):
            silence_end = starts[-1]
    return {"loudness_lufs": _number(_LOUDNESS.search(log)), "true_peak_db": _number(_PEAK.search(log)),
            "silence_start": silence_start, "silence_end": silence_end}


def peaks_from_pcm(pcm, buckets=PEAK_BUCKETS):
    """Mono s16le bytes → `buckets` values 0..255, scaled so the loudest bucket is 255."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return []
    count = min(buckets, len(samples))
    peaks = []
    for i in range(count):
        chunk = samples[len(samples) * i // count: len(samples) * (i + 1) // count]
        peaks.append(max(max(chunk), -min(chunk)))
    top = max(peaks) or 1
    return [round(255 * p / top) for p in peaks]


def analyze_file(path, ffmpeg="ffmpeg", on_process=None):
    """Run the single ffmpeg pass. Returns the dict that Analyzer stores; raises RuntimeError on failure."""
    command = [ffmpeg, "-nostdin", "-hide_banner", "-threads", "1", "-i", str(path),
               "-af", f"ebur128=peak=true,{SILENCE_FILTER}",
               "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
    lower_priority = (lambda: os.nice(19)) if hasattr(os, "nice") else None
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, preexec_fn=lower_priority)
    if on_process:
        on_process(process)
    log = []
    reader = threading.Thread(target=lambda: log.append(process.stderr.read()), daemon=True)
    reader.start()
    try:
        pcm = process.stdout.read()
        if process.wait(timeout=TIMEOUT) != 0:
            raise RuntimeError(f"ffmpeg завершился с кодом {process.returncode}")
    except subprocess.TimeoutExpired:
        process.kill()
        raise RuntimeError("ffmpeg не уложился в отведённое время")
    finally:
        reader.join(timeout=5)
    text = (log[0] if log else b"").decode("utf-8", "replace")
    duration = len(pcm) / 2 / SAMPLE_RATE
    if duration <= 0:
        raise RuntimeError("В файле не найдено аудио")
    result = parse_log(text, duration)
    result.update(duration=round(duration, 3), peaks=peaks_from_pcm(pcm))
    return result


class Analyzer:
    """One worker thread over a FIFO of track ids. `analyze` is injectable for tests."""

    def __init__(self, store, analyze=analyze_file):
        self.store = store
        self.analyze = analyze
        self.cond = threading.Condition()
        self.queue = deque()
        self.queued = set()
        self.running = None
        self.process = None
        self.stopping = False
        self.thread = None

    # --- public ----------------------------------------------------------------

    def enqueue(self, track_ids, front=False):
        with self.cond:
            for track_id in track_ids:
                if track_id in self.queued or track_id == self.running:
                    continue
                self.queued.add(track_id)
                (self.queue.appendleft if front else self.queue.append)(track_id)
            self.cond.notify()

    def enqueue_missing(self):
        """Every track without a result, including earlier failures: the startup backlog and the "missing" button."""
        tracks = self.store.snapshot()["tracks"]
        self.enqueue(sorted(tid for tid, t in tracks.items() if t["analyzed_at"] is None))

    def enqueue_all(self):
        self.enqueue(sorted(self.store.snapshot()["tracks"]))

    def discard(self, track_id):
        with self.cond:
            if track_id in self.queued:
                self.queued.discard(track_id)
                self.queue.remove(track_id)

    def status(self):
        with self.cond:
            return {"queued": len(self.queue), "running": self.running, "queue": list(self.queue)}

    def start(self):
        self.thread = threading.Thread(target=self._run, name="analyzer", daemon=True)
        self.thread.start()

    def stop(self):
        with self.cond:
            self.stopping = True
            process = self.process
            self.cond.notify_all()
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        if self.thread:
            self.thread.join(timeout=5)

    def run_pending(self):
        """Synchronous variant for tests: drain the queue on the calling thread."""
        while True:
            with self.cond:
                if not self.queue:
                    return
            self._step()

    # --- worker ----------------------------------------------------------------

    def _run(self):
        while True:
            with self.cond:
                while not self.queue and not self.stopping:
                    self.cond.wait()
                if self.stopping:
                    return
            self._step()

    def _step(self):
        with self.cond:
            track_id = self.queue.popleft()
            self.queued.discard(track_id)
            self.running = track_id
        try:
            track = self.store.snapshot()["tracks"].get(track_id)
            if track is None:
                return  # deleted while waiting
            path = self.store.root / "music" / track["file"]
            started = time.monotonic()
            try:
                result = self.analyze(path, on_process=self._register)
            except Exception as exc:
                if self.stopping:
                    return  # killed by shutdown, not a bad file: it is picked up again at the next start
                LOG.warning("Analysis failed track=%s: %s", track_id, exc)
                self.store.set_analysis_error(track_id, str(exc))
                return
            self.store.analysis_path(track_id).write_text(json.dumps({
                "duration": result["duration"], "peaks": result["peaks"],
                "silence_start": result["silence_start"], "silence_end": result["silence_end"]}))
            self.store.set_analysis(track_id, result["loudness_lufs"], result["true_peak_db"], result["duration"])
            LOG.info("Analyzed track=%s lufs=%s peak=%s duration=%.1f in %.1fs", track_id,
                     result["loudness_lufs"], result["true_peak_db"], result["duration"], time.monotonic() - started)
        finally:
            with self.cond:
                self.running = None
                self.process = None

    def _register(self, process):
        with self.cond:
            self.process = process
            if self.stopping:
                process.kill()
