#!/usr/bin/env bash
set -euo pipefail

BOT_INDEX="${1:?bot index is required}"
cd /home/Admin/musicbot

echo "Waiting for Lavalink..."
until nc -z 127.0.0.1 2333; do
  sleep 1
done
sleep "${MUSICBOT_START_DELAY_SECONDS:-10}"

echo "Starting musicbot ${BOT_INDEX}"
exec python3 main.py --bot-index "${BOT_INDEX}"
