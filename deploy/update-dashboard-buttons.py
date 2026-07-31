import asyncio
import json
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


async def main():
    with (BASE_DIR / "dashboard_state.json").open(encoding="utf-8") as stream:
        state = json.load(stream)

    token = os.getenv("DISCORD_TOKEN_1") or os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing the primary music-bot token")

    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Donate",
                    "emoji": {
                        "name": "1VIP",
                        "id": "1515580978376999004",
                        "animated": True,
                    },
                    "custom_id": "donate_btn",
                },
                {
                    "type": 2,
                    "style": 1,
                    "label": "คิวเพลง",
                    "emoji": {
                        "name": "0016",
                        "id": "1515992571854327908",
                        "animated": True,
                    },
                    "custom_id": "queue_btn",
                },
                {
                    "type": 2,
                    "style": 5,
                    "label": "เว็บของเรา",
                    "url": "https://www.khuiai.com/th/profile/Porkhyun",
                },
            ],
        }
    ]

    url = (
        "https://discord.com/api/v10/channels/"
        f"{state['channel_id']}/messages/{state['message_id']}"
    )
    headers = {"Authorization": f"Bot {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.patch(url, json={"components": components}) as response:
            if response.status != 200:
                detail = (await response.text())[:500]
                raise SystemExit(f"Discord returned HTTP {response.status}: {detail}")

    print("Dashboard buttons updated without restarting playback.")


if __name__ == "__main__":
    asyncio.run(main())
