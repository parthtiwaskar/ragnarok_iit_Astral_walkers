#!/bin/sh
# Hosted platforms (Render, Fly, ...) mount the persistent disk owned by root. If we start as root, take ownership of the
# data directory and then drop to the unprivileged app user before running anything. If we already start unprivileged
# (docker compose sets user: 10001), just run.
set -e
DATA="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  chown -R 10001:10001 "$DATA" 2>/dev/null || echo "warning: could not chown $DATA" >&2
  exec setpriv --reuid=10001 --regid=10001 --init-groups python -m nine_realms.server "$@"
fi
exec python -m nine_realms.server "$@"
