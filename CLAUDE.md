# CLAUDE.md

A physical NFC jukebox. You insert a "disc" carrying an NFC tag and the track bound to that tag plays. Controlled through a web UI on port 8080.

Based on the Hack Club project https://codex.hackclub.com/project-blogs/jukebox/ (Pi Zero 2 W, python-vlc, tag IDs hardcoded in an `if/elif` chain). Only the idea and the RC522 + MAX98357A hardware are kept; the code is a full rewrite.

## Hardware and system

- Raspberry Pi 3 Model B Rev 1.2, Debian 13 trixie (64-bit, kernel 6.18), Python 3.13, ffmpeg 7.1
- NFC: RC522 over SPI (`bus=0, device=0`, RST on BOARD pin 22), NTAG215 tags
- Audio: generic MAX98357A I2S amplifier (mono) + 4 Ω 3 W speaker. Wiring: BCLK→pin 12, LRC→pin 35, DIN→pin 40, Vin→5 V, GND, **SD→GPIO4 (pin 7)**, GAIN unconnected (9 dB). 1000 µF/16 V capacitor across amp Vin/GND
- The `max98357a` overlay drives SD_MODE on GPIO4: low (amp off) when idle, high (3.3 V = "left channel" mode) while playing. The ALSA `jukebox` device mixes (L+R)/2 into both channels, so the amp's channel mode does not matter
- Power is critical: an undersized PSU/cable caused instant brownout resets (`vcgencmd get_rsts` = 0x1000) right when a stream started. Check `vcgencmd get_throttled` stays `0x0` after hardware changes
- `/boot/firmware/config.txt` has `dtparam=spi=on`, `dtoverlay=max98357a`, `dtoverlay=disable-bt`; `vc4-kms-v3d`/`max_framebuffers` commented out, camera/display auto-detect off (backup: `config.txt.bak-20261013`). The installer never touches `config.txt`
- Headless since 2026-10-13: boots to `multi-user.target` (no lightdm/desktop, no HDMI card, so the amp is ALSA card 0, always addressed by name `MAX98357A`). Disabled: Raspberry Pi Connect, the user's PipeWire/WirePlumber/filter-chain/mpris-proxy (masked), bluetooth, rpcbind/NFS, udisks2, accounts-daemon, the serial console `serial-getty@ttyAMA0` (masked). Wi-Fi power save is off (`/etc/NetworkManager/conf.d/wifi-powersave-off.conf`). Keep avahi (`jukebox.local`), ssh, NetworkManager. Restore the desktop with `sudo systemctl set-default graphical.target`

## Files

- `core.py`: `Store` is SQLite (`jukebox.db`, WAL, `synchronous=FULL`, foreign keys, schema migrations in `MIGRATIONS` tracked by `PRAGMA user_version`). Reads go through a cached `snapshot()` dict (`tracks`, `tags` uid→{track, label}, `settings`); writes are explicit methods (`add_track`, `rename_track`, `delete_track`, `bind_tag`, `unbind_tag`, `set_tag_label`, `update_settings`) in `BEGIN IMMEDIATE` transactions. On first start it imports `library.json` and leaves the file in place for rollbacks. `Engine` is the playback/binding state machine; every method runs under `self.lock`
- `server.py`: FastAPI app from `create_app(engine)`, run by uvicorn from `main()`. Pydantic models define the API (also the source of the TS types). `worker()` is the NFC polling thread (~150 ms) that calls `engine.tick(uid)`; it starts and stops in the app lifespan. Serves the built frontend from `static/` at `/` (503 with a clear message if the build is missing), API docs at `/docs`
- `player.py`: `Player`. One long-lived `mpv --idle` process controlled over JSON IPC (Unix socket in the temp dir), output `--ao=alsa --audio-device=alsa/jukebox`. Decodes on the fly; `seek`, live `volume`, `time-pos`/`duration` come from mpv. `end-file` events are matched by `playlist_entry_id` so a replaced track's EOF is ignored. mpv is restarted lazily if it dies
- `reader.py`: `Reader` on top of `mfrc522==0.0.7` (REQA → Anticoll → HALT). HALT goes out with the Transmit command (the library's Transceive would busy-poll ~10 ms for a reply that never comes); an empty reader gets one attempt per poll, a present tag up to 3. ~5 ms per read with a tag, ~13 ms without; the service idles at ~9% of one core
- `frontend/`: React 19 + TypeScript 5 + Vite. `src/api/schema.d.ts` is generated (`npm run gen:api` → `scripts/dump_openapi.py`), never edited by hand; `src/api/client.ts` wraps it (`api.*`, XHR upload with progress). `src/useStatus.ts`: `useStatus` subscribes to `/api/events`, `usePlayback` extrapolates the position between events and after seeks, `useLabelPrompt` opens the disc-name dialog right after a binding, `useVolume` keeps the slider ahead of stale events. `src/components/`: `StatusStrip` (message, binding buttons, errors), `TrackList`/`TrackRow` (search, sort, inline rename, tag chips with labels, bind/unbind/delete, in-row seek bar), `PlayerBar` (sticky bottom bar), `Upload`, `SettingsPanel`, `LabelDialog`, `SeekBar`, `InlineEdit`. Delete/unbind confirmations use `window.confirm`. `npm run dev` proxies `/api` to the real Pi (`JUKEBOX_URL` overrides). TypeScript stays on 5.x: openapi-typescript needs the JS compiler API that TS 7 dropped
- `install.sh`: one-time Pi preparation (apt packages incl. mpv/ffmpeg, spi/gpio/audio groups, data dir); everything else comes from deploy
- `requirements.txt` (runtime: mfrc522, fastapi, uvicorn) and `requirements-dev.txt` (httpx2 for `fastapi.testclient`); both are installed on the Pi
- `tests/test_jukebox.py`: unittest with `FakePlayer`, `FakeChip`, `TestClient` and `tests/fake_mpv.py` (fake mpv IPC server; Player tests need Unix sockets, so they are skipped on Windows); no hardware needed. For a local UI check without the Pi, run the FastAPI app with a fake player and feed `engine.tick(uid)` from a script, then `npm run dev` with `JUKEBOX_URL=http://127.0.0.1:<port>`
- `deploy/asound.conf`: ALSA device `jukebox` = plug → route ((L+R)/2 to both channels) → plug → `hw:CARD=MAX98357A`. Installed to `/etc/asound.conf` by deploy
- `deploy/wireplumber-jukebox.conf`: disables the MAX98357A card in the desktop's WirePlumber so PipeWire never grabs it. Installed to `~/.config/wireplumber/wireplumber.conf.d/50-jukebox.conf`

## Data (`~/jukebox-data`)

- `jukebox.db` (+ `-wal`, `-shm`): tables `tracks(id sha256, title, file, original, created_at)`, `tags(uid, track_id → tracks ON DELETE SET NULL, label, created_at)`, `settings(key, value JSON)`. Back up with the service stopped, or with `sqlite3 jukebox.db ".backup copy.db"`
- `library.json`: legacy store, imported once into an empty DB and otherwise unused
- `music/`: originals stored as `<sha256>.<ext>`

## Engine logic

- `tick(uid)`: a missing tag shorter than `read_grace_ms` does not interrupt the track; the same tag returning within that window does not restart it
- `source`: `"tag"` or `"manual"`. After EOF a tag-started track repeats while the tag is present; a manual start plays once
- `suppressed`: uid whose autoplay is suppressed (after Stop, an error, or binding). Cleared when the tag is removed for longer than the grace period
- Binding: `arm` → `remove` phase (≥0.5 s with no tags) → `scan` → optional `conflict` → `confirm` → `save_binding`. 120 s timeout. Nothing autoplays after binding
- `continue_playback`: on tag removal the position is saved in `_resume_positions`. Kept in memory only; lost on restart

## API

Every mutating request (POST/PUT/DELETE) requires the `X-Jukebox: 1` header (CSRF guard). No authentication.
`GET /api/status`, `GET /api/events` (SSE: full status on every change, at least every 5 s; a seek does not push an event, the client extrapolates from the seek target). POST: `/api/play {track}`, `/api/stop`, `/api/seek {position}`, `/api/bind {track}`, `/api/confirm {uid, track}`, `/api/cancel`, `/api/unbind {uid}`, `/api/settings {volume, read_grace_ms, continue_playback}`, `/api/upload?name=...` (raw file bytes as body, up to 200 MB; mp3/flac/wav/ogg/m4a/aac/opus). `PUT /api/tags/{uid} {label}` (empty label forgets an unbound disc), `PUT /api/tracks/{id} {title}`, `DELETE /api/tracks/{id}` (stops it, deletes the file, unbinds its tags).
`status.tags` is `{uid: {track, label}}`; a labelled disc stays in the table with `track: null` after unbinding or deleting its track.

## Commands

```bash
python -m unittest discover -s tests -v   # from the repo root; works on Windows too (Player tests skipped)
cd frontend && npm run dev                 # UI dev server against the real Pi
cd frontend && npm run gen:api             # after changing API models in server.py
python3 server.py --data ~/jukebox-data --port 8080
journalctl -u jukebox-manager -n 80 --no-pager
sudo systemctl restart jukebox-manager
```

## Device and deploy

- Connection settings and password live in `.env` (gitignored; template in `.env.example`). The password is used for SSH and for sudo on the Pi
- Deploy: `./deploy.ps1` (flags `-DryRun`, `-SkipTests`, `-InstallKey`). Takes the working tree including uncommitted changes (`git ls-files -co --exclude-standard`), converts line endings to LF, uploads to `releases/<date>-<sha>[-dirty]`, runs tests on the Pi, switches the `current` symlink, restarts the service and checks `/api/status`, rolling back to the previous release on failure
- Pi-side logic: `deploy/remote.sh`. Unit template: `deploy/jukebox-manager.service` with `@APP_DIR@`-style placeholders
- Layout on the Pi: `~/jukebox-manager/{releases/, current -> releases/..., venv/}`. The venv is shared; `pip install` runs only when `requirements.txt` changes. `/etc/systemd/system/jukebox-manager.service.d/override.conf` holds a manual override (NoNewPrivileges=false, UMask=0022)
- The frontend is built locally: if `frontend/package.json` exists, deploy.ps1 runs `npm run build` and ships `frontend/dist` as `static/`. Never build on the Pi
- Manual SSH from Windows without sshpass: `askpass.cmd` containing `@echo <password>`, env `SSH_ASKPASS=<path>`, `SSH_ASKPASS_REQUIRE=force`, `DISPLAY=x`. scp does not expand `{a,b}`
- Deploy never touches `~/jukebox-data`
- Stop the service before manual hardware tests, otherwise two processes fight over RC522 and ALSA
- The journal is persistent (`/etc/systemd/journald.conf.d/50-persistent.conf`), so `journalctl -b -1` shows what happened before a reset

## Conventions

- Language: everything is in English (code, comments, docs, logs, scripts) except text shown in the web UI, which is Russian. That includes the backend strings that reach the UI: `engine.message`, `ValueError` messages (returned as HTTP 400), `reader_error`, `audio_error`
- Backend dependencies: `mfrc522`, `fastapi`, `uvicorn` (pinned). Do not add more without a good reason
- Call external processes (mpv, ffprobe) with argument lists, never through a shell

## Known issues

- `reader.py` returns only the 4 CL1 bytes. For NTAG215 that is `88` + 3 UID bytes (e.g. `885324B0`), not the full 7-byte UID
