# Jukebox Manager

Raspberry Pi 3 + RC522 + MAX98357A. Controlled from a browser, no access key.

## Behavior

- One disc is bound to one track.
- Read-dropout protection is a separate setting in milliseconds, 1000 ms by default.
- While the time since the last read is below that value, a missing tag does not interrupt playback. The same tag returning within that window does not restart the track.
- Once the window expires, tag-started playback stops. Re-inserting the disc starts the track from the beginning (or from the saved position when "continue playback" is enabled).
- When a tag-started track ends, the app checks (with the protection window) whether the tag is still there and starts the track again.
- "Stop" with a disc inserted silences the audio and suppresses autoplay. Resume with "Play", or remove the disc for longer than the protection window and insert it again.
- Without a disc, "Play" plays the track once. "Stop" ends it early.
- Inserting a different disc switches the track, including during playback started from the button.
- Starting, via the button, the track bound to the inserted disc behaves like tag playback from then on.
- Nothing plays after binding. Remove the tag, wait for the protection window to pass, and insert it again.
- The active track's button turns into "Stop" and its row is highlighted, including for tag playback. The state refreshes about once a second.

## Inputs, outputs and edge cases

Inputs: one NFC tag near the RC522, local audio files, and browser actions. Outputs: sound on the MAX98357A, state/errors in the UI and the service journal, stored files and bindings in `~/jukebox-data`.

An unknown tag stops the previous track and is shown in the UI. Reading several tags at once is not supported. Player errors are shown in the browser and the journal; endless restart attempts of a broken file are suppressed, and "Play" lets you retry. A reader failure does not prevent manual playback without a disc.

## Deploying

From the development machine (Windows, PowerShell):

```powershell
Copy-Item .env.example .env   # once; fill in host, user and password
./deploy.ps1                  # -DryRun to only build the archive, -SkipTests to skip tests on the Pi
```

The script uploads the working tree to `~/jukebox-manager/releases/<id>` on the Pi, runs the tests there, switches the `current` symlink, restarts `jukebox-manager` and rolls back if the service does not answer. Music and bindings in `~/jukebox-data` are never touched.

## Fresh install

Audio and SPI must be configured beforehand (`dtparam=spi=on`, `dtoverlay=max98357a` in `config.txt`). The installer does not change `config.txt` or Wi-Fi.

```bash
tar -xzf ~/jukebox-manager.tar.gz -C ~
cd ~/jukebox-manager
bash install.sh
```

The installer creates the Python environment, installs ffmpeg and mfrc522, adds the user to the spi/gpio/audio groups, and enables the service. Run it as your normal user, not via `sudo bash`.

## Adding tracks

1. Open the web UI.
2. Choose one or more audio files to upload.
3. Click "Bind tag" on the track.
4. Move all tags away from the reader, wait for the prompt, and present the tag.
5. If the tag is already bound to another track, confirm the replacement or cancel.
6. After saving, remove and re-insert the disc.

Nothing is written to the NFC tag. The tag UID is mapped to a file on the SD card. Several tags can be bound to the same track. A binding can be removed independently of the file.

Formats: MP3, FLAC, WAV, OGG, M4A, AAC, OPUS, up to 200 MB per file. Identical files are not duplicated; different files with the same name are stored separately. Decoding errors show up at playback time.

## Audio

The path verified on the device: a pre-rendered WAV, PCM S16_LE, 48 kHz, stereo with silence on the left and the source's right channel on the right. Playback via aplay with buffer-size=48000 and period-size=12000. This temporarily uses only the right channel of the source rather than a stereo mix.

FFmpeg prepares the WAV before playback starts; there is no streaming decode. Prepared files are kept in `~/jukebox-data/pcm-cache`. The first play of a new file may be delayed by preparation. WAV takes more space than MP3. The cache can be deleted while the service is stopped and will be rebuilt. Source tracks are never modified.

Volume applies from the next track start, as noted in the UI. Each volume value produces its own WAV version.

On the user's device the prepared WAV played through aplay with these buffers without hiss. The root cause of the channel issue has not been identified yet.

## Diagnostics

```bash
journalctl -u jukebox-manager -n 80 --no-pager
sudo systemctl status jukebox-manager --no-pager
```

Stop the service before standalone hardware tests so two processes don't share the RC522 or the audio output:

```bash
sudo systemctl stop jukebox-manager
```

Afterwards:

```bash
sudo systemctl start jukebox-manager
```

## Data

`~/jukebox-data/library.json` stores settings and bindings; `~/jukebox-data/music` stores the files. For a backup, stop the service and copy the whole `~/jukebox-data`. JSON is saved through a temporary file and an atomic replace.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covered: read dropouts, repeat after track end, one-shot manual play, manual stop with a disc inserted, disc swap, preservation of existing bindings, access without a key, tag UID reading, communication errors, and timeouts.

Dependencies and docs:
- https://github.com/pimylifeup/MFRC522-python (mfrc522 0.0.7, GPLv3)
