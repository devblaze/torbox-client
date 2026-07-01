#!/usr/bin/env sh
# Drop privileges to a configurable user (Unraid / linuxserver.io style) so that
# files written to /downloads and /data are owned the way Sonarr/Radarr expect.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"

# Remap the bundled "app" user/group to the requested ids (-o allows non-unique).
groupmod -o -g "$PGID" app 2>/dev/null || groupadd -o -g "$PGID" app
usermod  -o -u "$PUID" -g "$PGID" app 2>/dev/null || \
    useradd -o -u "$PUID" -g "$PGID" -d /app -s /usr/sbin/nologin app

umask "$UMASK"

mkdir -p "${DATA_DIR:-/data}" "${DOWNLOAD_DIR:-/downloads}"
# Small state dir: safe to chown recursively.
chown -R "$PUID:$PGID" "${DATA_DIR:-/data}" 2>/dev/null || true
# Downloads may be huge and shared with other apps: only claim the mountpoint,
# not everything inside it. New files inherit PUID via the process below.
chown "$PUID:$PGID" "${DOWNLOAD_DIR:-/downloads}" 2>/dev/null || true

echo "Starting torbox-client as UID=$PUID GID=$PGID UMASK=$UMASK"
exec gosu "$PUID:$PGID" "$@"
