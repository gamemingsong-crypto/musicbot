# Pork Hyun Music Bot

Discord music system with four bots, one Lavalink node, shared dashboard, queue recovery, Spotify/Apple Music/Deezer playlist import, SponsorBlock, and lyrics.

## Commands

- `!play <name or URL>`: Play or queue a track, album, or playlist.
- `!queue`: Show the queue and select a queued track to play now.
- `!nowplaying`: Show the current track, progress, volume, and loop mode.
- `!pause` / `!resume`: Pause or resume playback.
- `!volume <0-150>`: Read or change the volume.
- `!loop off|track|queue`: Disable looping, repeat one track, or repeat the queue.
- `!shuffle`: Shuffle queued tracks.
- `!remove <position>`: Remove a queued track by its displayed number.
- `!skip`: Vote to skip the current track.
- `!forceskip`: Force a skip (Manage Server permission required).
- `/lyrics`: Show lyrics for the current track privately with Discord's Close Message control.
- `!lyrics`: Open a requester-only button for the same private lyrics view.
- `!sponsorblock`: Show SponsorBlock status.
- `!sponsorblock on|off`: Change SponsorBlock status (Manage Server permission required).
- `!clear`: Clear queued tracks.
- `!stop`: Clear the queue and disconnect the bot.
- `!musichelp`: Show the command list in Discord.

Music commands are accepted only in the configured request channel. Each bot is assigned to one configured Music Room.

## Local checks

```bash
python3 -m py_compile main.py
python3 -m unittest discover -s tests -v
```

## Configuration

Copy `.env.example` to `.env` and fill in the tokens and Lavalink password. Never commit `.env` or `lavalink.env`.

The production Lavalink configuration is in `deploy/application.yml`. Its secrets are read from environment variables:

- `LAVALINK_PASSWORD`
- `YOUTUBE_REFRESH_TOKEN`
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`

Lavalink listens on `127.0.0.1:2333`, so its API is not exposed to the internet.

## Production launchers

- Copy `deploy/start-musicbot.sh` and `deploy/start-1.sh` through `start-4.sh` into `/home/Admin/musicbot/`.
- Copy `deploy/start-lavalink.sh` to `/home/Admin/start-lavalink.sh`.
- Copy `deploy/application.yml` to `/home/Admin/application.yml`.
- Keep `/home/Admin/lavalink.env` and `/home/Admin/musicbot/.env` at permission mode `600`.

The launchers use `exec` so PM2 signals reach Python and Java, allowing Discord, Wavelink, and aiohttp sessions to close cleanly.
