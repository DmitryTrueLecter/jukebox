import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from analysis import Analyzer
from core import Engine, Store

LOG = logging.getLogger("jukebox")
MAX_UPLOAD = 200 * 1024 * 1024
EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus"}
HERE = Path(__file__).parent


# --- uploads ---------------------------------------------------------------

def upload_name(name):
    name = name.replace("\\", "/").split("/")[-1]
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise ValueError("Поддерживаются MP3, FLAC, WAV, OGG, M4A, AAC, OPUS")
    return name


def register_upload(store, temp, digest, name):
    """Validate a fully received upload and add it to the library. Removes temp."""
    try:
        try:
            result = subprocess.run([
                "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                "-of", "json", str(temp)
            ], capture_output=True, text=True, timeout=30, check=True)
        except subprocess.CalledProcessError as exc:
            raise ValueError("Файл не удалось распознать как аудио") from exc
        if not json.loads(result.stdout).get("streams"):
            raise ValueError("В файле не найдена аудиодорожка")
        with store.lock:
            existing = store.snapshot()["tracks"].get(digest)
            if existing and (store.root / "music" / existing["file"]).is_file():
                return {"id": digest, "duplicate": True}
            filename = digest + Path(name).suffix.lower()
            os.replace(temp, store.root / "music" / filename)
            store.add_track(digest, Path(name).stem[:240], filename, name[:255])
        return {"id": digest, "duplicate": False}
    finally:
        temp.unlink(missing_ok=True)


# --- API models ------------------------------------------------------------

class Track(BaseModel):
    title: str
    file: str
    original: str
    created_at: float
    # analysis (None until the background queue has processed the file)
    loudness_lufs: Optional[float]
    true_peak_db: Optional[float]
    duration: Optional[float]
    analyzed_at: Optional[float]
    analysis_error: Optional[str]
    gain_db: float = 0.0  # automatic normalization gain for the current settings; computed, not stored
    # user adjustments
    gain_offset_db: float = 0.0
    trim_start: Optional[float]
    trim_end: Optional[float]


class Tag(BaseModel):
    """A disc. `track` is None for a labelled disc that is not bound to anything."""
    track: Optional[str]
    label: Optional[str]


class Settings(BaseModel):
    volume: int = Field(ge=0, le=100)
    read_grace_ms: int = Field(ge=1, le=60000)
    continue_playback: bool = False
    normalize: bool = True
    target_lufs: float = Field(default=-16.0, ge=-30, le=-5)


class Analysis(BaseModel):
    queued: int
    running: Optional[str]


class Pending(BaseModel):
    track: str
    phase: Literal["remove", "scan", "conflict"]
    empty_since: Optional[float] = None
    deadline: float
    uid: Optional[str] = None
    old_track: Optional[str] = None


class Status(BaseModel):
    version: int
    tracks: dict[str, Track]
    tags: dict[str, Tag]
    settings: Settings
    current_tag: Optional[str]
    last_tag: Optional[str]
    selected_track: Optional[str]
    playing_track: Optional[str]
    source: Optional[Literal["tag", "manual"]]
    pending: Optional[Pending]
    message: str
    reader_error: Optional[str]
    audio_error: Optional[str]
    position: Optional[float]
    duration: Optional[float]
    analysis: Analysis


class TrackRef(BaseModel):
    track: str


class Play(BaseModel):
    track: str
    position: Optional[float] = Field(default=None, ge=0)


class AnalyzeRequest(BaseModel):
    """No fields: analyze what is missing. `track`: re-run that one. `all`: re-run the whole library."""
    track: Optional[str] = None
    all: bool = False


class Waveform(BaseModel):
    duration: float
    peaks: list[int]
    silence_start: Optional[float]
    silence_end: Optional[float]


class TagRef(BaseModel):
    uid: str


class Confirm(BaseModel):
    uid: str
    track: str


class Seek(BaseModel):
    position: float


class Label(BaseModel):
    label: str = Field(max_length=80)


class TrackUpdate(BaseModel):
    """Fields left out are not changed; trim_start/trim_end set to null together clear the trim."""
    title: Optional[str] = Field(default=None, max_length=240)
    gain_offset_db: Optional[float] = None
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None


class Ok(BaseModel):
    ok: bool = True


class Uploaded(BaseModel):
    id: str
    duplicate: bool


# --- app -------------------------------------------------------------------

def require_jukebox_header(x_jukebox: Optional[str] = Header(default=None)):
    # Cross-site forms cannot set custom headers, so this blocks CSRF.
    if x_jukebox != "1":
        raise HTTPException(403, "Недопустимый запрос")


def status_key(status):
    """Everything except the ever-changing position: a change means "push an event"."""
    return json.dumps({k: v for k, v in status.items() if k != "position"}, sort_keys=True, default=str)


def create_app(engine, lifespan=None, static_dir=HERE / "static"):
    app = FastAPI(title="Jukebox", lifespan=lifespan)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)  # also covers 404/405 from routing
    async def http_error(request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", [])[1:])
        return JSONResponse({"error": f"Некорректный запрос: {field} {first.get('msg', '')}".strip()},
                            status_code=400)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        if request.method != "GET":
            LOG.info("%s %s %s", request.method, request.url.path, response.status_code)
        return response

    api = APIRouter(prefix="/api")
    # Every mutating route (POST/PUT/DELETE) lives on this router and needs the X-Jukebox header.
    post = APIRouter(prefix="/api", dependencies=[Depends(require_jukebox_header)])

    @api.get("/status", response_model=Status)
    def status():
        return engine.status()

    @api.get("/events")
    async def events(request: Request):
        """Server-sent events: the full status whenever it changes, and every 5 s for position sync."""
        async def stream():
            last_key, last_sent = None, 0.0
            while not await request.is_disconnected():
                current = await run_in_threadpool(engine.status)
                key = status_key(current)
                if key != last_key or time.monotonic() - last_sent >= 5:
                    yield f"data: {json.dumps(current, ensure_ascii=False)}\n\n"
                    last_key, last_sent = key, time.monotonic()
                await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @post.post("/play", response_model=Ok)
    def play(body: Play):
        engine.manual_play(body.track, body.position)
        return Ok()

    @post.post("/stop", response_model=Ok)
    def stop():
        engine.manual_stop()
        return Ok()

    @post.post("/seek", response_model=Ok)
    def seek(body: Seek):
        engine.seek(body.position)
        return Ok()

    @post.post("/bind", response_model=Ok)
    def bind(body: TrackRef):
        engine.arm(body.track)
        return Ok()

    @post.post("/confirm", response_model=Ok)
    def confirm(body: Confirm):
        engine.confirm(body.uid, body.track)
        return Ok()

    @post.post("/cancel", response_model=Ok)
    def cancel():
        engine.cancel()
        return Ok()

    @post.post("/unbind", response_model=Ok)
    def unbind(body: TagRef):
        engine.unbind(body.uid)
        return Ok()

    @post.put("/tags/{uid}", response_model=Ok)
    def set_label(uid: str, body: Label):
        engine.set_label(uid, body.label)
        return Ok()

    @post.put("/tracks/{track_id}", response_model=Ok)
    def update_track(track_id: str, body: TrackUpdate):
        given = body.model_fields_set
        if "title" in given:
            engine.rename(track_id, body.title or "")
        sound = {k: getattr(body, k) for k in ("gain_offset_db", "trim_start", "trim_end") if k in given}
        if sound:
            clear = "trim_start" in given and "trim_end" in given and body.trim_start is None and body.trim_end is None
            engine.set_sound(track_id, gain_offset_db=sound.get("gain_offset_db"), trim_start=sound.get("trim_start"),
                             trim_end=sound.get("trim_end"), clear_trim=clear)
        return Ok()

    @api.get("/tracks/{track_id}/waveform", response_model=Waveform)
    def waveform(track_id: str):
        path = engine.store.analysis_path(track_id)
        if track_id not in engine.store.snapshot()["tracks"] or not path.is_file():
            raise HTTPException(404, "Трек ещё не проанализирован")
        return json.loads(path.read_text())

    @post.post("/analyze", response_model=Ok)
    def analyze(body: AnalyzeRequest):
        if engine.analyzer is None:
            raise ValueError("Анализ недоступен")
        if body.track:
            if body.track not in engine.store.snapshot()["tracks"]:
                raise ValueError("Трек не найден")
            engine.analyzer.enqueue([body.track], front=True)
        elif body.all:
            engine.analyzer.enqueue_all()
        else:
            engine.analyzer.enqueue_missing()
        return Ok()

    @post.delete("/tracks/{track_id}", response_model=Ok)
    def delete_track(track_id: str):
        if engine.analyzer:
            engine.analyzer.discard(track_id)
        engine.delete_track(track_id)
        return Ok()

    @post.post("/settings", response_model=Ok)
    def settings(body: Settings):
        with engine.lock:
            engine.player.volume(body.volume)
            engine.store.update_settings(**body.model_dump())
        return Ok()

    @post.post("/upload", response_model=Uploaded)
    async def upload(request: Request, name: str):
        """Raw file bytes as the body, original file name in ?name=."""
        name = upload_name(name)
        length = int(request.headers.get("content-length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError("Максимальный размер одного файла: 200 МБ")
        store = engine.store
        temp = store.root / "music" / (uuid.uuid4().hex + ".part")
        digest, received = hashlib.sha256(), 0
        try:
            with temp.open("xb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > length:
                        raise ValueError("Размер файла не совпадает с заявленным")
                    output.write(chunk)
                    digest.update(chunk)
                if received != length:
                    raise ValueError("Загрузка оборвалась, повтори её")
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        result = await run_in_threadpool(register_upload, store, temp, digest.hexdigest(), name)
        if engine.analyzer and store.snapshot()["tracks"][result["id"]]["analyzed_at"] is None:
            engine.analyzer.enqueue([result["id"]])
        return result

    app.include_router(api)
    app.include_router(post)

    if (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:
        @app.get("/", include_in_schema=False)
        def root():
            # deploy.ps1 ships frontend/dist as static/; a release without it has no UI
            raise HTTPException(503, "Веб-интерфейс не собран: static/index.html отсутствует")

    return app


# --- NFC polling -------------------------------------------------------------

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
    analyzer = Analyzer(store)
    engine = Engine(store, player, analyzer=analyzer)
    stopping = threading.Event()
    try:
        from reader import Reader
        reader = Reader()
    except Exception as exc:
        LOG.warning("Reader init failed in main thread: %s", exc)
        reader = None
    thread = threading.Thread(target=worker, args=(engine, stopping, reader), daemon=True)

    @asynccontextmanager
    async def lifespan(app):
        thread.start()
        analyzer.enqueue_missing()
        analyzer.start()
        LOG.info("Web interface on port %s", args.port)
        yield
        stopping.set()
        analyzer.stop()
        thread.join(timeout=5)
        engine.player.close()
        if reader:
            reader.close()

    uvicorn.run(create_app(engine, lifespan), host=args.host, port=args.port,
                access_log=False, log_level="warning", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
