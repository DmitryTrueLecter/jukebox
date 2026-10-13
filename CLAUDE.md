# CLAUDE.md

Физический NFC-jukebox. Пользователь вставляет «диск» с NFC-меткой, и играет привязанный к ней трек. Управление через веб-интерфейс на порту 8080.

За основу взят проект Hack Club: https://codex.hackclub.com/project-blogs/jukebox/ (Pi Zero 2 W, python-vlc, ID меток зашиты в `if/elif`). От оригинала осталась только идея и железо RC522 + MAX98357A. Код полностью переписан.

## Железо и система

- Raspberry Pi 3 Model B Rev 1.2, Debian 13 trixie (64-бит, ядро 6.18), Python 3.13, ffmpeg 7.1
- NFC: RC522 по SPI (`bus=0, device=0`, RST на пине 22 в нумерации BOARD). Метки NTAG215
- Звук: I2S-усилитель MAX98357A (моно), ALSA-устройство `plughw:CARD=MAX98357A,DEV=0`
- В `/boot/firmware/config.txt` есть `dtparam=spi=on` и `dtoverlay=max98357a`. `asound.conf` нет. Установщик `config.txt` не трогает

## Файлы

- `core.py`: `Store` хранит `library.json` с атомарной записью (tmp + fsync + os.replace + fsync каталога). `Engine` — конечный автомат воспроизведения и привязки, все методы под `self.lock`
- `server.py`: `ThreadingHTTPServer`, API, загрузка файлов, `worker()` — цикл чтения NFC каждые ~150 мс, вызывает `engine.tick(uid)`
- `player.py`: `Player`. ffmpeg заранее готовит WAV в `pcm-cache`, затем `aplay` его играет. Seek/resume реализованы через подачу WAV в pipe с пропуском байтов
- `reader.py`: `Reader` на базе библиотеки `mfrc522==0.0.7` (REQA → Anticoll → HALT)
- `index.html`: одностраничный интерфейс без сборки и зависимостей. JS в одну строку, опрашивает `/api/status` примерно раз в секунду
- `install.sh`: venv, apt-пакеты, группы spi/gpio/audio, systemd-юнит `jukebox-manager.service`
- `tests/test_jukebox.py`: unittest с `FakePlayer` и `FakeChip`, железо не нужно

## Данные (`~/jukebox-data`)

- `library.json`: `{"version": 1, "tracks": {sha256: {title, file, original}}, "tags": {uid: track_id}, "settings": {volume, read_grace_ms, continue_playback}}`
- `music/`: исходники под именем `<sha256>.<ext>`
- `pcm-cache/`: `<stem>-<size>-<mtime_ns>-v<volume>-right.wav`. Можно удалить при остановленной службе

## Логика Engine

- `tick(uid)`: отсутствие метки короче `read_grace_ms` не прерывает трек. Возврат той же метки в этом окне трек не перезапускает
- `source`: `"tag"` или `"manual"`. После EOF трек от метки повторяется, если метка на месте. Ручной запуск играет один раз
- `suppressed`: uid, для которого автозапуск подавлен (после «Стопа», ошибки или привязки). Сбрасывается, когда метку вынимают дольше, чем на grace
- Привязка: `arm` → фаза `remove` (≥0.5 с без меток) → `scan` → при необходимости `conflict` → `confirm` → `save_binding`. Таймаут 120 с. После привязки трек сам не запускается
- `continue_playback`: при снятии метки позиция сохраняется в `_resume_positions`. Хранится только в памяти и пропадает при рестарте

## API

Все POST требуют заголовок `X-Jukebox: 1` (защита от CSRF). Авторизации нет.
`GET /api/status`. POST: `/api/play {track}`, `/api/stop`, `/api/seek {position}`, `/api/bind {track}`, `/api/confirm {uid, track}`, `/api/cancel`, `/api/unbind {uid}`, `/api/settings {volume, read_grace_ms, continue_playback}`, `/api/upload?name=...` (тело — сырые байты файла, до 200 МБ; форматы mp3/flac/wav/ogg/m4a/aac/opus).

## Команды

```bash
python3 -m unittest discover -s tests -v   # запускать на Pi или в WSL: на Windows fsync каталога не работает
python3 server.py --data ~/jukebox-data --port 8080
journalctl -u jukebox-manager -n 80 --no-pager
sudo systemctl restart jukebox-manager
```

## Устройство и деплой

- SSH: `jukebox@jukebox.local`. Пароль даёт пользователь, в репозиторий его не записывать
- С Windows без sshpass: `askpass.cmd` с `@echo <пароль>` в scratchpad, переменные `SSH_ASKPASS=<путь>`, `SSH_ASKPASS_REQUIRE=force`, `DISPLAY=x`. scp не понимает `{a,b}`, файлы копировать по одному
- Приложение: `~/jukebox-manager` (с `.venv`), служба `jukebox-manager.service`, данные в `~/jukebox-data`
- Деплой: остановить службу, скопировать `*.py`, `index.html` и `tests/` в `~/jukebox-manager`, запустить службу. Данные не трогать
- Перед ручными тестами железа останавливать службу, иначе два процесса будут делить RC522 и ALSA

## Соглашения

- Сообщения в интерфейсе и ошибки (`ValueError`) на русском. Ошибки из `ValueError` уходят клиенту как HTTP 400
- Только стандартная библиотека Python плюс `mfrc522`. Новые зависимости не добавлять без необходимости
- Внешние процессы (ffmpeg, ffprobe, aplay) вызывать списком аргументов, без shell

## Известные проблемы

- `reader.py` возвращает только 4 байта CL1. У NTAG215 это `88` + 3 байта UID (например, `885324B0`), а не полный 7-байтовый UID. README в этом месте неточен
- `server.py`, `log_message`: при таймауте до разбора запроса нет `self.command`, и в журнал сыплется `AttributeError`
- Кэш WAV не чистится: каждое значение громкости создаёт отдельную копию каждого трека
- Звук идёт только из правого канала исходника (левый заглушён), это временное решение после аппаратных проверок
- `install.sh` ставит лишний `mpv`. На Pi остались `~/jukebox-venv`, `tmpo7oy32sf/`, `access-key.txt`, настройка `remove_delay`
- `vcgencmd get_throttled` = `0x50000`: были просадки питания и троттлинг. Стоит проверить блок питания
