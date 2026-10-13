# Web UI roadmap

Agreed on 2026-10-13. The web UI text stays Russian; everything else is in English.

## Decisions

- Frontend: React + TypeScript 5 + Vite in `frontend/`, built on the dev machine by `deploy.ps1`. Never built on the Pi.
- **Desktop-first** layout. The UI is mostly used from a computer, but it must still work on a phone.
- Backend: FastAPI + uvicorn. Pydantic models are the API contract; TS types are generated from them (`npm run gen:api`).
- Live updates: SSE (`/api/events`). The client extrapolates playback position between events.
- Storage: SQLite (`jukebox.db`, WAL, `synchronous=FULL`, migrations via `PRAGMA user_version`). Waveform/analysis data goes to files next to the music, not into the DB.
- Every phase is deployed and checked on the device before the next one starts.

## Phase 1: foundation (done 2026-10-13)

- SQLite store with a one-time import of `library.json` (the file is kept for rollbacks).
- FastAPI backend with the same endpoints and error format, plus SSE and static serving. Legacy UI at `/legacy`, API docs at `/docs`.
- React skeleton: track list with play/stop, status message, bottom bar with the current time.

## Phase 2: UI parity and first features (next)

Goal: the React UI replaces `index.html` completely, then `index.html` and `/legacy` are deleted.

Parity with the legacy UI:
- Upload (multiple files, progress, duplicate notice, errors).
- Tag binding flow: arm → "remove all tags" → "present the tag" → conflict confirm/cancel, 120 s timeout. Shows `pending` from status.
- Unbind a tag.
- Settings: volume (applies live), read grace ms, continue playback.
- Status, reader/audio errors, unknown tag, connection state.

New features:
1. **Player in the track row + sticky bottom bar.** The playing row expands with a seekable progress bar and times. The fixed bottom bar shows title, source (disc or button), play/stop, seek bar, volume. Smooth position via `usePosition`.
2. **Tag labels** ("красно-желтый"). Backend: `tags.label` already exists. Add `PUT /api/tags/{uid}` (label), return tags as objects in status (`{uid: {track, label}}`), and update Engine/legacy accordingly. Unbinding keeps the label (set `track_id` NULL instead of deleting the row) so a disc keeps its name. Ask for a label right after binding; the unknown-tag message shows the label when known. Optional colour swatch per tag.
3. **Delete track.** `DELETE /api/tracks/{id}`: stop it if it is playing, remove the file, unbind its tags (keep the labelled tag rows), drop resume positions. Confirmation in the UI.

Open questions for phase 2:
- Sorting/search in the track list (title, recently added)?
- Rename tracks (the title currently comes from the file name)?

## Phase 3: per-track sound

Background analysis queue: one worker at `nice 19`, so playback is never affected. It runs on upload and once for the existing library.
- ffmpeg `ebur128`/`loudnorm` analysis: integrated LUFS and true peak.
- Waveform peaks (~1000 points) stored as a file per track.
- `silencedetect`: suggested start/end.

4. **Loudness.** Auto gain = target LUFS − measured, capped so true peak stays below −1 dBTP. Add a manual ±dB offset per track. mpv gets `af=volume=<gain>dB` per file in `loadfile` options; the global volume stays separate and live. Pick the target (≈ −16 LUFS) by ear on the device.
5. **Non-destructive trim.** `start`/`end` per track, passed as mpv `start=`/`end=`. The original file is never touched. UI: waveform with two handles, "listen from here", and a one-click button to use the silencedetect suggestion. Seek, resume and the progress bar work relative to the trimmed range. Optional short fade-in.

New DB columns (migration 2): `tracks.gain_db`, `tracks.gain_offset_db`, `tracks.trim_start`, `tracks.trim_end`, `tracks.loudness_lufs`, `tracks.true_peak_db`, `tracks.analyzed_at`.

## Known follow-ups (not scheduled)

- NFC polling still costs ~9–12% of a core when no tag is present. A custom wait loop that honours the RC522 timer IRQ would cut it further.
- `reader.py` returns only the 4 CL1 bytes of 7-byte UIDs.
- Power-cut resilience: consider a read-only root (overlayfs) with only `~/jukebox-data` writable.
- `continue_playback` positions live in memory only and are lost on restart; they could move to SQLite.
