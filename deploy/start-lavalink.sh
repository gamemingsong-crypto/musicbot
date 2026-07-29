#!/usr/bin/env bash
set -euo pipefail

cd /home/Admin
set -a
source /home/Admin/lavalink.env
set +a

exec java -Xms512M -Xmx1024M -jar Lavalink.jar
