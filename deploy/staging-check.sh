#!/usr/bin/env bash
set -euo pipefail

cd /home/Admin/lavalink-staging
sed -i '/oauth:/,/skipInitialization:/ {
  s/enabled: true/enabled: false/
  /refreshToken:/d
  /skipInitialization:/d
}' application.yml
rm -f staging.log

SERVER_PORT=2444 \
LAVALINK_PASSWORD=stage-only \
SPOTIFY_CLIENT_ID=dummy \
SPOTIFY_CLIENT_SECRET=dummy \
timeout 45 java -Xms128M -Xmx384M -jar /home/Admin/Lavalink.jar > staging.log 2>&1 &

for _ in $(seq 1 25); do
  if nc -z 127.0.0.1 2444; then
    break
  fi
  sleep 1
done

curl -fsS -H 'Authorization: stage-only' http://127.0.0.1:2444/version
echo
grep -E 'Found plugin|Loading.*Plugin|Started Launcher|ERROR|Exception' staging.log | tail -80
fuser -k 2444/tcp >/dev/null 2>&1 || true
