import copy
import json
import logging
import os
import threading
import time
from pathlib import Path


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "music").mkdir(exist_ok=True)
        self.path = self.root / "library.json"
        self.lock = threading.RLock()
        self.data = {"version": 1, "tracks": {}, "tags": {},
                     "settings": {"volume": 35, "read_grace_ms": 1000, "continue_playback": False}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("version") != 1:
                raise ValueError("Неподдерживаемая версия library.json")

        if "read_grace_ms" not in self.data["settings"]:
            self.update(lambda d: d["settings"].update(read_grace_ms=1000))
        if "continue_playback" not in self.data["settings"]:
            self.update(lambda d: d["settings"].update(continue_playback=False))

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def update(self, change):
        with self.lock:
            updated = copy.deepcopy(self.data)
            result = change(updated)
            temp = self.path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(updated, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.data = updated
            return result


class Engine:
    def __init__(self, store, player, clock=time.monotonic):
        self.store, self.player, self.clock = store, player, clock
        self.lock = threading.RLock()
        self.current = None
        self.track = None
        self.last_seen = 0.0
        self.last_uid = None
        self.pending = None
        self.message = "Поднеси диск к считывателю"
        self.reader_error = None
        self.audio_error = None
        self.source = None
        self.suppressed = None
        self._resume_positions = {}  # {uid: (track_id, elapsed_seconds)}

    def stop(self):
        self.player.stop()
        self.track = None
        self.source = None

    def start(self, track_id, source):
        data = self.store.snapshot()
        if track_id not in data["tracks"]:
            raise ValueError("Трек не найден")
        track = data["tracks"][track_id]
        path = self.store.root / "music" / track["file"]
        if not path.is_file():
            raise ValueError("Файл трека отсутствует")
        offset = 0.0
        if source == "tag" and data["settings"].get("continue_playback"):
            saved = self._resume_positions.get(self.current)
            if saved and saved[0] == track_id:
                offset = saved[1]
                self._resume_positions.pop(self.current, None)
        self.player.play(path, offset=offset)
        self.track, self.source = track_id, source
        if source == "tag":
            self.last_seen = max(self.last_seen, self.clock())
        self.audio_error = None
        self.message = "Воспроизведение: " + track["title"]
        logging.info("PLAY track=%s source=%s tag=%s offset=%.1f", track_id, source, self.current, offset)

    def manual_play(self, track_id):
        with self.lock:
            if self.pending:
                raise ValueError("Сначала заверши или отмени привязку")
            self.suppressed = self.current
            mapped = self.store.snapshot()["tags"].get(self.current)
            source = "tag" if mapped == track_id else "manual"
            if source == "tag":
                self.suppressed = None
            self.start(track_id, source)

    def manual_stop(self):
        with self.lock:
            self.suppressed = self.current
            self._resume_positions.pop(self.current, None)
            self.stop()
            self.message = "Остановлено кнопкой"

    def arm(self, track_id):
        with self.lock:
            if track_id not in self.store.snapshot()["tracks"]:
                raise ValueError("Трек не найден")
            self.stop()
            self.pending = {"track": track_id, "phase": "remove", "empty_since": None,
                            "deadline": self.clock() + 120}
            self.current = None
            self.message = "Убери все метки от считывателя"

    def save_binding(self, uid, track_id):
        self.store.update(lambda d: d["tags"].__setitem__(uid, track_id))
        self.pending = None
        self.current = uid
        self.suppressed = uid
        self.last_seen = self.clock()
        self.message = f"Метка {uid} привязана. Вынь и вставь диск для запуска."

    def confirm(self, uid, track_id):
        with self.lock:
            p = self.pending
            if not p or p["phase"] != "conflict" or p.get("uid") != uid or p["track"] != track_id:
                raise ValueError("Запрос привязки устарел")
            if self.clock() >= p["deadline"]:
                self.pending = None
                raise ValueError("Время привязки истекло")
            self.save_binding(uid, track_id)

    def cancel(self):
        with self.lock:
            self.pending = None
            self.current = self.last_uid
            self.last_seen = self.clock()
            self.message = "Привязка отменена. Вынь и вставь диск для запуска."

    def seek(self, position):
        with self.lock:
            if self.track is None:
                raise ValueError("Ничего не воспроизводится")
            data = self.store.snapshot()
            track = data["tracks"][self.track]
            path = self.store.root / "music" / track["file"]
            self.player.play(path, offset=max(0.0, position))
            self.audio_error = None

    def unbind(self, uid):
        with self.lock:
            self.store.update(lambda d: d["tags"].pop(uid, None))
            self._resume_positions.pop(uid, None)
            if self.current == uid:
                self.stop()
            self.message = "Привязка удалена, трек сохранён"

    def tick(self, uid, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            data = self.store.snapshot()
            delay = data["settings"]["read_grace_ms"] / 1000
            if uid:
                self.last_uid = uid
            p = self.pending
            if p:
                if now >= p["deadline"]:
                    self.pending = None
                    self.current = uid
                    self.suppressed = uid
                    self.last_seen = now
                    self.message = "Время привязки истекло"
                elif p["phase"] == "remove":
                    if uid:
                        p["empty_since"] = None
                    elif p["empty_since"] is None:
                        p["empty_since"] = now
                    elif now - p["empty_since"] >= 0.5:
                        p["phase"] = "scan"
                        self.message = "Поднеси метку для выбранного трека"
                elif p["phase"] == "scan" and uid:
                    if uid in data["tags"] and data["tags"][uid] != p["track"]:
                        p.update(phase="conflict", uid=uid, old_track=data["tags"][uid])
                        self.message = "Метка уже занята. Подтверди замену или отмени."
                    else:
                        self.save_binding(uid, p["track"])
                return

            if self.current and now - self.last_seen >= delay:
                if self.source == "tag":
                    if data["settings"].get("continue_playback") and self.track:
                        uid_for_resume = self.current
                        track_for_resume = self.track
                        self.stop()
                        self._resume_positions[uid_for_resume] = (track_for_resume, self.player.elapsed)
                    else:
                        self.stop()
                self.current = None
                self.suppressed = None
                self.message = "Диск извлечён"
            if uid:
                self.last_seen = now
                if uid != self.current:
                    self.current = uid
                    self.suppressed = None
                    self.stop()
                    track_id = data["tags"].get(uid)
                    if track_id:
                        self.start(track_id, "tag")
                    else:
                        self.message = f"Неизвестная метка: {uid}"
            try:
                ended = self.player.poll() == "eof"
            except Exception as exc:
                self.audio_error = str(exc)
                self.suppressed = self.current
                self.stop()
                self.message = "Ошибка воспроизведения"
                return
            if ended:
                previous_source = self.source
                self.track = None
                self.source = None
                if self.current:
                    self._resume_positions.pop(self.current, None)
                if (previous_source == "tag" and self.current
                        and self.current != self.suppressed
                        and now - self.last_seen < delay):
                    track_id = data["tags"].get(self.current)
                    if track_id:
                        self.start(track_id, "tag")
                else:
                    self.message = "Трек завершён"

    def status(self):
        with self.lock:
            return {**self.store.snapshot(), "current_tag": self.current,
                    "last_tag": self.last_uid, "selected_track": self.track, "playing_track": self.track, "source": self.source,
                    "pending": copy.deepcopy(self.pending), "message": self.message,
                    "reader_error": self.reader_error, "audio_error": self.audio_error,
                    "position": self.player.position, "duration": self.player.duration}
