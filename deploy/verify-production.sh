#!/usr/bin/env bash
set -euo pipefail

set -a
source /home/Admin/lavalink.env
set +a

AUTH_HEADER="Authorization: $LAVALINK_PASSWORD"
BASE_URL="http://127.0.0.1:2333"

echo "Lavalink version: $(curl -fsS -H "$AUTH_HEADER" "$BASE_URL/version")"
curl -fsS -H "$AUTH_HEADER" "$BASE_URL/v4/info" >/tmp/lavalink-info.json
python3 - <<'PY'
import json

with open("/tmp/lavalink-info.json", encoding="utf-8") as stream:
    info = json.load(stream)

plugins = {plugin["name"]: plugin["version"] for plugin in info.get("plugins", [])}
required = {"youtube-plugin", "lavasrc-plugin", "sponsorblock-plugin", "java-lyrics-plugin"}
missing = required.difference(plugins)
print("Plugins:", ", ".join(f"{name}={version}" for name, version in sorted(plugins.items())))
if missing:
    raise SystemExit(f"Missing plugins: {', '.join(sorted(missing))}")
PY

curl -fsS -G -H "$AUTH_HEADER" \
  --data-urlencode "identifier=ytsearch:Never Gonna Give You Up Rick Astley" \
  "$BASE_URL/v4/loadtracks" >/tmp/lavalink-search.json
python3 - <<'PY'
import json

with open("/tmp/lavalink-search.json", encoding="utf-8") as stream:
    result = json.load(stream)

load_type = result.get("loadType")
data = result.get("data")
track_count = len(data) if isinstance(data, list) else int(bool(data))
print(f"YouTube search: {load_type}, tracks={track_count}")
if load_type in {None, "empty", "error"} or track_count < 1:
    raise SystemExit("YouTube search health check failed")
PY

echo "Listening socket:"
ss -ltnp | grep ':2333 '

echo "Secret file modes:"
stat -c '%a %n' /home/Admin/application.yml /home/Admin/lavalink.env /home/Admin/musicbot/.env

echo "Music processes:"
pm2 jlist | node -e '
const fs = require("fs");
const names = new Set(["lavalin", "musicbot", "musicbot-2", "musicbot-3", "musicbot-4"]);
for (const app of JSON.parse(fs.readFileSync(0, "utf8"))) {
  if (names.has(app.name)) console.log(`${app.name}: ${app.pm2_env.status}, pid=${app.pid}, restarts=${app.pm2_env.restart_time}`);
}
'
