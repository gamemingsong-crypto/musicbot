#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/Admin/musicbot
LAVALINK_DIR=/home/Admin
BACKUP_DIR="/home/Admin/backups/music-upgrade-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
cp -a "$LAVALINK_DIR/application.yml" "$BACKUP_DIR/application.yml"
cp -a "$APP_DIR/.env" "$BACKUP_DIR/musicbot.env"
cp -a "$APP_DIR/main.py" "$BACKUP_DIR/main.py"
cp -a "$APP_DIR"/start-*.sh "$BACKUP_DIR/" 2>/dev/null || true
cp -a /home/Admin/.pm2/dump.pm2 "$BACKUP_DIR/dump.pm2" 2>/dev/null || true

extract_value() {
  local key="$1"
  awk -v key="$key" '
    $1 == key ":" {
      sub(/^[^:]*:[[:space:]]*/, "")
      gsub(/^"|"$/, "")
      print
      exit
    }
  ' "$LAVALINK_DIR/application.yml"
}

youtube_refresh_token="$(extract_value refreshToken)"
spotify_client_id="$(extract_value clientId)"
spotify_client_secret="$(extract_value clientSecret)"

if [[ -z "$youtube_refresh_token" || -z "$spotify_client_id" || -z "$spotify_client_secret" ]]; then
  echo "Could not migrate the existing Lavalink credentials; production was not changed." >&2
  exit 1
fi

umask 077
lavalink_password="$(openssl rand -hex 32)"
{
  printf 'LAVALINK_PASSWORD=%s\n' "$lavalink_password"
  printf 'YOUTUBE_REFRESH_TOKEN=%s\n' "$youtube_refresh_token"
  printf 'SPOTIFY_CLIENT_ID=%s\n' "$spotify_client_id"
  printf 'SPOTIFY_CLIENT_SECRET=%s\n' "$spotify_client_secret"
} > "$LAVALINK_DIR/lavalink.env"
chmod 600 "$LAVALINK_DIR/lavalink.env"

env_tmp="$(mktemp)"
grep -vE '^(LAVALINK_URI|LAVALINK_PASSWORD|SPONSORBLOCK_CATEGORIES)=' "$APP_DIR/.env" > "$env_tmp" || true
{
  cat "$env_tmp"
  printf 'LAVALINK_URI=http://127.0.0.1:2333\n'
  printf 'LAVALINK_PASSWORD=%s\n' "$lavalink_password"
  printf 'SPONSORBLOCK_CATEGORIES=sponsor,selfpromo,interaction\n'
} > "$APP_DIR/.env"
rm -f "$env_tmp"
chmod 600 "$APP_DIR/.env"

install -m 600 "$APP_DIR/deploy/application.yml" "$LAVALINK_DIR/application.yml"
install -m 700 "$APP_DIR/deploy/start-lavalink.sh" "$LAVALINK_DIR/start-lavalink.sh"
install -m 700 "$APP_DIR/deploy/start-musicbot.sh" "$APP_DIR/start-musicbot.sh"
for index in 1 2 3 4; do
  install -m 700 "$APP_DIR/deploy/start-${index}.sh" "$APP_DIR/start-${index}.sh"
done

echo "Production files installed. Backup: $BACKUP_DIR"
