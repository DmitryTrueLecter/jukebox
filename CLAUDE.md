# CLAUDE.md

A physical NFC jukebox. You insert a "disc" carrying an NFC tag and the track bound to that tag plays. Controlled through a web UI on port 8080.

Based on the Hack Club project https://codex.hackclub.com/project-blogs/jukebox/ (Pi Zero 2 W, python-vlc, tag IDs hardcoded in an `if/elif` chain). Only the idea and the RC522 + MAX98357A hardware are kept; the code is a full rewrite.

## Hardware and system

- Raspberry Pi 3 Model B Rev 1.2, Debian 13 trixie (64-bit, kernel 6.18), Python 3.13, ffmpeg 7.1
- NFC: RC522 over SPI (`bus=0, device=0`, RST on BOARD pin 22), NTAG215 tags
- Audio: MAX98357A I2S amplifier (mono), ALSA device `plughw:CARD=MAX98357A,DEV=0`
- `/boot/firmware/config.txt` has `dtparam=spi=on` and `dtoverlay=max98357a`. No `asound.conf`. The installer never touches `config.txt`

## Files

- `core.py`: `Store` holds `library.json` with atomic writes (tmp + fsync + os.replace + directory fsync). `Engine` is the playback/binding state machine; every method runs under `self.lock`
- `server.py`: `ThreadingHTTPServer`, API, uploads, and `worker()`, the NFC polling loop (~150 ms) that calls `engine.tick(uid)`
- `player.py`: `Player`. ffmpeg pre-renders a WAV into `pcm-cache`, then `aplay` plays it. Seek/resume feed the WAV through a pipe, skipping bytes
- `reader.py`: `Reader` on top of `mfrc522==0.0.7` (REQA → Anticoll → HALT)
- `index.html`: single-page UI, no build step, no dependencies. One-line JS, polls `/api/status` about once a second
- `install.sh`: venv, apt packages, spi/gpio/audio groups, systemd unit `jukebox-manager.service`
- `tests/test_jukebox.py`: unittest with `FakePlayer` and `FakeChip`; no hardware needed

## Data (`~/jukebox-data`)

- `library.json`: `{"version": 1, "tracks": {sha256: {title, file, original}}, "tags": {uid: track_id}, "settings": {volume, read_grace_ms, continue_playback}}`
- `music/`: originals stored as `<sha256>.<ext>`
- `pcm-cache/`: `<stem>-<size>-<mtime_ns>-v<volume>-right.wav`. Safe to delete while the service is stopped

## Engine logic

- `tick(uid)`: a missing tag shorter than `read_grace_ms` does not interrupt the track; the same tag returning within that window does not restart it
- `source`: `"tag"` or `"manual"`. After EOF a tag-started track repeats while the tag is present; a manual start plays once
- `suppressed`: uid whose autoplay is suppressed (after Stop, an error, or binding). Cleared when the tag is removed for longer than the grace period
- Binding: `arm` → `remove` phase (≥0.5 s with no tags) → `scan` → optional `conflict` → `confirm` → `save_binding`. 120 s timeout. Nothing autoplays after binding
- `continue_playback`: on tag removal the position is saved in `_resume_positions`. Kept in memory only; lost on restart

## API

Every POST requires the `X-Jukebox: 1` header (CSRF guard). No authentication.
`GET /api/status`. POST: `/api/play {track}`, `/api/stop`, `/api/seek {position}`, `/api/bind {track}`, `/api/confirm {uid, track}`, `/api/cancel`, `/api/unbind {uid}`, `/api/settings {volume, read_grace_ms, continue_playback}`, `/api/upload?name=...` (raw file bytes as body, up to 200 MB; mp3/flac/wav/ogg/m4a/aac/opus).

## Commands

```bash
python3 -m unittest discover -s tests -v   # run on the Pi or WSL: directory fsync fails on Windows
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

## Conventions

- Language: everything is in English (code, comments, docs, logs, scripts) except text shown in the web UI, which is Russian. That includes `index.html` and the backend strings that reach the UI: `engine.message`, `ValueError` messages (returned as HTTP 400), `reader_error`, `audio_error`
- Python standard library plus `mfrc522` only. Do not add dependencies without a good reason
- Call external processes (ffmpeg, ffprobe, aplay) with argument lists, never through a shell

## Known issues

- `reader.py` returns only the 4 CL1 bytes. For NTAG215 that is `88` + 3 UID bytes (e.g. `885324B0`), not the full 7-byte UID
- `server.py` `log_message`: on a timeout before the request line is parsed there is no `self.command`, so an `AttributeError` lands in the journal
- The WAV cache is never pruned: each volume value creates its own copy of every track
- Only the right channel of the source is played (left is muted); a temporary workaround after hardware tests
- `install.sh` still writes the old-layout unit and installs an unused `mpv`. Leftovers on the Pi: `~/jukebox-venv`, `tmpo7oy32sf/`, old files and `.venv` in the root of `~/jukebox-manager`, `access-key.txt`, the `remove_delay` setting
- `vcgencmd get_throttled` = `0x50000`: under-voltage and throttling have occurred. Check the power supply
