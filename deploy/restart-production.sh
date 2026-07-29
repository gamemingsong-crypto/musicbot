#!/usr/bin/env bash
set -euo pipefail

LAVALINK_ENV="/home/Admin/lavalink.env"
MUSIC_APPS=(musicbot musicbot-2 musicbot-3 musicbot-4)

if [[ ! -r "$LAVALINK_ENV" ]]; then
  echo "Missing $LAVALINK_ENV" >&2
  exit 1
fi

set -a
source "$LAVALINK_ENV"
set +a

if [[ -z "${LAVALINK_PASSWORD:-}" ]]; then
  echo "LAVALINK_PASSWORD is missing" >&2
  exit 1
fi

echo "Stopping music bots..."
pm2 stop "${MUSIC_APPS[@]}"

echo "Starting the secured Lavalink process..."
pm2 delete lavalin >/dev/null 2>&1 || true
pm2 start /home/Admin/start-lavalink.sh --name lavalin --interpreter none

healthy=0
for _ in $(seq 1 90); do
  if curl -fsS \
    -H "Authorization: $LAVALINK_PASSWORD" \
    http://127.0.0.1:2333/version >/tmp/lavalink-version; then
    healthy=1
    break
  fi
  sleep 1
done

if [[ "$healthy" -ne 1 ]]; then
  echo "Lavalink did not become healthy in 90 seconds." >&2
  pm2 logs lavalin --lines 100 --nostream >&2 || true
  exit 1
fi

echo "Lavalink $(cat /tmp/lavalink-version) is healthy."
echo "Starting music bots..."
pm2 restart "${MUSIC_APPS[@]}" --update-env
pm2 save

sleep 20
pm2 status
