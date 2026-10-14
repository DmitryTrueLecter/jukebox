import copy
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

LOG = logging.getLogger("jukebox")

DEFAULT_SETTINGS = {"volume": 35, "read_grace_ms": 1000, "continue_playback": False}

# Index i holds the migration from schema version i to i + 1.
MIGRATIONS = [
    """
    CREATE TABLE tracks (
        id TEXT PRIMARY KEY,            -- sha256 of the file
        title TEXT NOT NULL,
        file TEXT NOT NULL,             -- name inside music/
        original TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    );
    CREATE TABLE tags (
        uid TEXT PRIMARY KEY,
        track_id TEXT REFERENCES tracks(id) ON DELETE SET NULL,
        label TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL             -- JSON
    );
    """,
]


class Store:
    """Library in SQLite (jukebox.db). Reads go through an in-memory snapshot."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "music").mkdir(exist_ok=True)
        self.path = self.root / "jukebox.db"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # survive the plug being pulled
        self.db.execute("PRAGMA foreign_keys=ON")
        self._cache = None
        self._migrate()
        self._import_json()

    @contextmanager
    def _tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            self._cache = None

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise ValueError(f"jukebox.db schema {version} is newer than this code")
        for target in range(version, len(MIGRATIONS)):
            with self._tx() as db:
                for statement in MIGRATIONS[target].split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA user_version={target + 1}")
            LOG.info("DB migrated to schema %s", target + 1)

    def _import_json(self):
        legacy = self.root / "library.json"
        if not legacy.exists() or self.db.execute(
                "SELECT 1 FROM settings UNION ALL SELECT 1 FROM tracks LIMIT 1").fetchone():
            return
        data = json.loads(legacy.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("Unsupported library.json version")
        now = time.time()
        with self._tx() as db:
            for track_id, track in data["tracks"].items():
                db.execute("INSERT INTO tracks VALUES (?, ?, ?, ?, ?)",
                           (track_id, track["title"], track["file"], track.get("original", ""), now))
            for uid, track_id in data["tags"].items():
                if track_id in data["tracks"]:  # an unlabelled tag without a track has nothing to keep
                    db.execute("INSERT INTO tags VALUES (?, ?, NULL, ?)", (uid, track_id, now))
            settings = {**DEFAULT_SETTINGS,
                        **{k: v for k, v in data["settings"].items() if k in DEFAULT_SETTINGS}}
            for key, value in settings.items():
                db.execute("INSERT INTO settings VALUES (?, ?)", (key, json.dumps(value)))
        # library.json stays untouched so a rollback to a pre-SQLite release still finds it.
        LOG.info("Imported library.json: %s tracks, %s tags", len(data["tracks"]), len(data["tags"]))

    def snapshot(self):
        """{"tracks": {id: {...}}, "tags": {uid: {"track": id | None, "label": str | None}}, "settings": {...}}.

        Tags include labelled discs that are not bound to any track; unlabelled unbound tags are not stored.
        """
        with self.lock:
            if self._cache is None:
                tracks = {row[0]: {"title": row[1], "file": row[2], "original": row[3], "created_at": row[4]}
                          for row in self.db.execute(
                              "SELECT id, title, file, original, created_at FROM tracks ORDER BY title")}
                tags = {row[0]: {"track": row[1], "label": row[2]}
                        for row in self.db.execute("SELECT uid, track_id, label FROM tags ORDER BY uid")}
                settings = {**DEFAULT_SETTINGS,
                            **{k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM settings")}}
                self._cache = {"version": 1, "tracks": tracks, "tags": tags, "settings": settings}
            return copy.deepcopy(self._cache)

    def add_track(self, track_id, title, file, original):
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO tracks VALUES (?, ?, ?, ?, ?)",
                       (track_id, title, file, original, time.time()))

    def rename_track(self, track_id, title):
        with self._tx() as db:
            db.execute("UPDATE tracks SET title = ? WHERE id = ?", (title, track_id))

    def delete_track(self, track_id):
        """Remove the track row; its tags are unbound (labelled ones stay). Returns the file name."""
        with self._tx() as db:
            row = db.execute("SELECT file FROM tracks WHERE id = ?", (track_id,)).fetchone()
            if row is None:
                raise ValueError("Трек не найден")
            db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))  # tags.track_id -> NULL
            db.execute("DELETE FROM tags WHERE track_id IS NULL AND label IS NULL")
        return row[0]

    def bind_tag(self, uid, track_id):
        with self._tx() as db:
            db.execute("INSERT INTO tags VALUES (?, ?, NULL, ?) "
                       "ON CONFLICT(uid) DO UPDATE SET track_id = excluded.track_id",
                       (uid, track_id, time.time()))

    def unbind_tag(self, uid):
        """Drop the track binding; a labelled disc keeps its row (and name)."""
        with self._tx() as db:
            db.execute("UPDATE tags SET track_id = NULL WHERE uid = ?", (uid,))
            db.execute("DELETE FROM tags WHERE uid = ? AND label IS NULL", (uid,))

    def set_tag_label(self, uid, label):
        label = label.strip() or None
        with self._tx() as db:
            db.execute("INSERT INTO tags VALUES (?, NULL, ?, ?) "
                       "ON CONFLICT(uid) DO UPDATE SET label = excluded.label",
                       (uid, label, time.time()))
            db.execute("DELETE FROM tags WHERE uid = ? AND track_id IS NULL AND label IS NULL", (uid,))

    def update_settings(self, **values):
        with self._tx() as db:
            for key, value in values.items():
                db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))


def bound_track(data, uid):
    """Track id bound to the tag, or None (unknown tag or a labelled disc without a track)."""
    tag = data["tags"].get(uid)
    return tag["track"] if tag else None


def tag_name(data, uid):
    """What the UI calls the disc: its label when it has one, otherwise the UID."""
    tag = data["tags"].get(uid)
    return (tag and tag["label"]) or uid


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
            mapped = bound_track(self.store.snapshot(), self.current)
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
        self.store.bind_tag(uid, track_id)
        self.pending = None
        self.current = uid
        self.suppressed = uid
        self.last_seen = self.clock()
        name = tag_name(self.store.snapshot(), uid)
        self.message = f"Метка {name} привязана. Вынь и вставь диск для запуска."

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
            self.player.seek(max(0.0, position))

    def unbind(self, uid):
        with self.lock:
            self.store.unbind_tag(uid)
            self._resume_positions.pop(uid, None)
            if self.current == uid:
                self.stop()
            self.message = "Привязка удалена, трек сохранён"

    def set_label(self, uid, label):
        with self.lock:
            if len(label.strip()) > 80:
                raise ValueError("Название диска: не больше 80 символов")
            self.store.set_tag_label(uid, label)

    def rename(self, track_id, title):
        with self.lock:
            title = " ".join(title.split())
            if not title:
                raise ValueError("Название не может быть пустым")
            if len(title) > 240:
                raise ValueError("Название: не больше 240 символов")
            if track_id not in self.store.snapshot()["tracks"]:
                raise ValueError("Трек не найден")
            self.store.rename_track(track_id, title)
            if self.track == track_id:
                self.message = "Воспроизведение: " + title

    def delete_track(self, track_id):
        """Stop it if needed, forget it, remove the file. Labelled discs survive unbound."""
        with self.lock:
            track = self.store.snapshot()["tracks"].get(track_id)
            if track is None:
                raise ValueError("Трек не найден")
            if self.pending and self.pending["track"] == track_id:
                self.cancel()
            if self.track == track_id:
                self.suppressed = self.current
                self.stop()
            self._resume_positions = {uid: saved for uid, saved in self._resume_positions.items()
                                      if saved[0] != track_id}
            file = self.store.delete_track(track_id)
            (self.store.root / "music" / file).unlink(missing_ok=True)
            self.message = f"Трек «{track['title']}» удалён"
            logging.info("DELETE track=%s file=%s", track_id, file)

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
                    old_track = bound_track(data, uid)
                    if old_track and old_track != p["track"]:
                        p.update(phase="conflict", uid=uid, old_track=old_track)
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
                    track_id = bound_track(data, uid)
                    if track_id:
                        self.start(track_id, "tag")
                    else:
                        self.message = f"Неизвестная метка: {tag_name(data, uid)}"
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
                    track_id = bound_track(data, self.current)
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
