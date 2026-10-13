# CLAUDE.md

A physical NFC jukebox. You insert a "disc" carrying an NFC tag and the track bound to that tag plays. Controlled through a web UI on port 8080.

Based on the Hack Club project https://codex.hackclub.com/project-blogs/jukebox/ (Pi Zero 2 W, python-vlc, tag IDs hardcoded in an `if/elif` chain). Only the idea and the RC522 + MAX98357A hardware are kept; the code is a full rewrite.

## Hardware and system

- Raspberry Pi 3 Model B Rev 1.2, Debian 13 trixie (64-bit, kernel 6.18), Python 3.13, ffmpeg 7.1
- NFC: RC522 over SPI (`bus=0, device=0`, RST on BOARD pin 22), NTAG215 tags
- Audio: generic MAX98357A I2S amplifier (mono) + 4 Ω 3 W speaker. Wiring: BCLK→pin 12, LRC→pin 35, DIN→pin 40, Vin→5 V, GND, **SD→GPIO4 (pin 7)**, GAIN unconnected (9 dB). 1000 µF/16 V capacitor across amp Vin/GND
- The `max98357a` overlay drives SD_MODE on GPIO4: low (amp off) when idle, high (3.3 V = "left channel" mode) while playing. The ALSA `jukebox` device mixes (L+R)/2 into both channels, so the amp's channel mode does not matter
- Power is critical: an undersized PSU/cable caused instant brownout resets (`vcgencmd get_rsts` = 0x1000) right when a stream started. Check `vcgencmd get_throttled` stays `0x0` after hardware changes
- `/boot/firmware/config.txt` has `dtparam=spi=on` and `dtoverlay=max98357a`. No `asound.conf`. The installer never touches `config.txt`

## Files

- `core.py`: `Store` holds `library.json` with atomic writes (tmp + fsync + os.replace + directory fsync). `Engine` is the playback/binding state machine; every method runs under `self.lock`
- `server.py`: `ThreadingHTTPServer`, API, uploads, and `worker()`, the NFC polling loop (~150 ms) that calls `engine.tick(uid)`
- `player.py`: `Player`. One long-lived `mpv --idle` process controlled over JSON IPC (Unix socket in the temp dir), output `--ao=alsa --audio-device=alsa/jukebox`. Decodes on the fly; `seek`, live `volume`, `time-pos`/`duration` come from mpv. `end-file` events are matched by `playlist_entry_id` so a replaced track's EOF is ignored. mpv is restarted lazily if it dies
- `reader.py`: `Reader` on top of `mfrc522==0.0.7` (REQA → Anticoll → HALT)
- `index.html`: single-page UI, no build step, no dependencies. One-line JS, polls `/api/status` about once a second
- `install.sh`: venv, apt packages, spi/gpio/audio groups, systemd unit `jukebox-manager.service`
- `tests/test_jukebox.py`: unittest with `FakePlayer`, `FakeChip` and `tests/fake_mpv.py` (fake mpv IPC server; Player tests need Unix sockets, so they are skipped on Windows); no hardware needed
- `deploy/asound.conf`: ALSA device `jukebox` = plug → route ((L+R)/2 to both channels) → plug → `hw:CARD=MAX98357A`. Installed to `/etc/asound.conf` by deploy
- `deploy/wireplumber-jukebox.conf`: disables the MAX98357A card in the desktop's WirePlumber so PipeWire never grabs it. Installed to `~/.config/wireplumber/wireplumber.conf.d/50-jukebox.conf`

## Data (`~/jukebox-data`)

- `library.json`: `{"version": 1, "tracks": {sha256: {title, file, original}}, "tags": {uid: track_id}, "settings": {volume, read_grace_ms, continue_playback}}`
- `music/`: originals stored as `<sha256>.<ext>`

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
- The journal is persistent (`/etc/systemd/journald.conf.d/50-persistent.conf`), so `journalctl -b -1` shows what happened before a reset

## Conventions

- Language: everything is in English (code, comments, docs, logs, scripts) except text shown in the web UI, which is Russian. That includes `index.html` and the backend strings that reach the UI: `engine.message`, `ValueError` messages (returned as HTTP 400), `reader_error`, `audio_error`
- Python standard library plus `mfrc522` only. Do not add dependencies without a good reason
- Call external processes (mpv, ffprobe) with argument lists, never through a shell

## Known issues

- `reader.py` returns only the 4 CL1 bytes. For NTAG215 that is `88` + 3 UID bytes (e.g. `885324B0`), not the full 7-byte UID
- `server.py` `log_message`: on a timeout before the request line is parsed there is no `self.command`, so an `AttributeError` lands in the journal
- Leftovers on the Pi: `~/jukebox-venv`, `tmpo7oy32sf/`, old files and `.venv` in the root of `~/jukebox-manager`, `~/jukebox-manager.service.bak`, `access-key.txt`, the `remove_delay` setting
