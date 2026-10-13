#!/usr/bin/env bash
# Runs on the Pi from inside a freshly extracted release directory.
# Env: APP_DIR DATA_DIR PORT KEEP RELEASE RUN_TESTS. Sudo password (may be empty) on stdin.
set -euo pipefail

IFS= read -r PW || true
PW=${PW%$'\r'}

as_root() {
    if sudo -n true 2>/dev/null; then
        sudo "$@"
    elif [ -n "$PW" ]; then
        printf '%s\n' "$PW" | sudo -S -p '' "$@"
    else
        echo "sudo needs a password: set JUKEBOX_PASSWORD in .env" >&2
        exit 1
    fi
}

release_dir="$APP_DIR/releases/$RELEASE"
venv="$APP_DIR/venv"
unit=/etc/systemd/system/jukebox-manager.service
cd "$release_dir"

echo "==> venv"
if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
fi
req_hash=$(cat requirements.txt requirements-dev.txt | sha256sum | cut -d' ' -f1)
if [ "$(cat "$venv/.requirements.sha256" 2>/dev/null)" != "$req_hash" ]; then
    "$venv/bin/pip" install -q -r requirements.txt -r requirements-dev.txt
    echo "$req_hash" > "$venv/.requirements.sha256"
else
    echo "    requirements unchanged"
fi

if [ "$RUN_TESTS" = "1" ]; then
    echo "==> tests"
    if ! "$venv/bin/python" -m unittest discover -s tests -q; then
        rm -rf -- "$release_dir"
        echo "!! tests failed, release removed, service untouched" >&2
        exit 1
    fi
fi

echo "==> systemd unit"
rendered=$(sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@DATA_DIR@|$DATA_DIR|g" \
               -e "s|@USER@|$(id -un)|g" -e "s|@PORT@|$PORT|g" deploy/jukebox-manager.service)
if [ "$rendered" != "$(cat "$unit" 2>/dev/null)" ]; then
    tmp=$(mktemp)
    printf '%s\n' "$rendered" > "$tmp"
    as_root install -m 644 "$tmp" "$unit"
    rm -f "$tmp"
    as_root systemctl daemon-reload
    as_root systemctl enable -q jukebox-manager.service
    echo "    updated"
else
    echo "    unchanged"
fi

echo "==> ALSA config"
if ! cmp -s deploy/asound.conf /etc/asound.conf; then
    as_root install -m 644 deploy/asound.conf /etc/asound.conf
    echo "    updated"
else
    echo "    unchanged"
fi

echo "==> WirePlumber rule"
wp_dir="$HOME/.config/wireplumber/wireplumber.conf.d"
wp_conf="$wp_dir/50-jukebox.conf"
if ! cmp -s deploy/wireplumber-jukebox.conf "$wp_conf"; then
    mkdir -p "$wp_dir"
    install -m 644 deploy/wireplumber-jukebox.conf "$wp_conf"
    runtime="/run/user/$(id -u)"
    if [ -d "$runtime" ] && XDG_RUNTIME_DIR="$runtime" systemctl --user is-active -q wireplumber; then
        XDG_RUNTIME_DIR="$runtime" systemctl --user restart wireplumber
        echo "    updated, wireplumber restarted"
    else
        echo "    updated (wireplumber not running)"
    fi
else
    echo "    unchanged"
fi

previous=$(readlink "$APP_DIR/current" 2>/dev/null || true)

switch_to() {
    ln -sfn "$1" "$APP_DIR/current.tmp"
    mv -Tf "$APP_DIR/current.tmp" "$APP_DIR/current"
    as_root systemctl restart jukebox-manager.service
}

healthy() {
    for _ in $(seq 1 20); do
        sleep 1
        if curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
            return 0
        fi
    done
    return 1
}

echo "==> switching to $RELEASE"
switch_to "releases/$RELEASE"
if ! healthy; then
    echo "!! service does not answer /api/status" >&2
    journalctl -u jukebox-manager -n 30 --no-pager >&2 || true
    if [ -n "$previous" ]; then
        echo "!! rolling back to $previous" >&2
        switch_to "$previous"
        healthy || echo "!! service still not answering after rollback" >&2
    fi
    exit 1
fi

echo "==> pruning old releases (keeping $KEEP)"
cd "$APP_DIR/releases"
ls -1 | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
    if [ "releases/$old" != "$previous" ]; then
        rm -rf -- "$old"
    fi
done

echo "==> done: $RELEASE"
