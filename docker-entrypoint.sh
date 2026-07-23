#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID" in
    ''|*[!0-9]*)
        echo "PUID must be a non-negative numeric user ID; received: $PUID" >&2
        exit 1
        ;;
esac

case "$PGID" in
    ''|*[!0-9]*)
        echo "PGID must be a non-negative numeric group ID; received: $PGID" >&2
        exit 1
        ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    if [ "$(id -u)" -ne "$PUID" ] || [ "$(id -g)" -ne "$PGID" ]; then
        echo "Warning: the container was started as $(id -u):$(id -g); PUID=$PUID and PGID=$PGID cannot be applied without root startup privileges." >&2
    fi
    exec "$@"
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd --gid "$PGID" melodarr
fi

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd \
        --no-create-home \
        --no-log-init \
        --uid "$PUID" \
        --gid "$PGID" \
        --home-dir /app/data \
        --shell /usr/sbin/nologin \
        melodarr
fi

mkdir -p /app/data
chown -R "$PUID:$PGID" /app/data

export HOME=/app/data
exec gosu "$PUID:$PGID" "$@"
