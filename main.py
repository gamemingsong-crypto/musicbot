import discord
from discord.ext import commands, tasks
import wavelink
import os
import re
import json
import aiohttp
import argparse
import asyncio
import time
from urllib.parse import parse_qs, quote, urlparse
from dotenv import load_dotenv

# โหลดตัวแปรลับจาก .env และให้ค่าในไฟล์ชนะ env เก่าที่ PM2 อาจ cache ไว้
load_dotenv(override=True)

RUNTIME_ARG_PARSER = argparse.ArgumentParser(add_help=False)
RUNTIME_ARG_PARSER.add_argument("--bot-index", type=int, default=int(os.getenv("BOT_INDEX", "1")))
RUNTIME_ARG_PARSER.add_argument("--token-env", default=os.getenv("DISCORD_TOKEN_ENV"))
RUNTIME_ARGS, _ = RUNTIME_ARG_PARSER.parse_known_args()


def parse_channel_ids(value: str | None) -> tuple[int, ...]:
    channel_ids = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            channel_ids.append(int(item))
        except ValueError:
            print(f"Ignoring invalid Discord channel ID: {item}")
    return tuple(channel_ids)


def parse_channel_names(value: str | None, defaults: tuple[str, ...]) -> tuple[str, ...]:
    names = tuple(item.strip() for item in (value or "").split(",") if item.strip())
    return names or defaults


def normalized_channel_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


MUSIC_REQUEST_CHANNEL_IDS = parse_channel_ids(os.getenv("MUSIC_REQUEST_CHANNEL_IDS"))
MUSIC_REQUEST_CHANNEL_NAMES = parse_channel_names(
    os.getenv("MUSIC_REQUEST_CHANNEL_NAMES"),
    ("ห้องขอเพลง",),
)
MUSIC_VOICE_CHANNEL_IDS = parse_channel_ids(os.getenv("MUSIC_VOICE_CHANNEL_IDS")) or (
    1522556101256806491,  # Music Room 1
    1508703259592753242,  # Music Room 2
    1508703233370226843,  # Music Room 3
    1519909824538738759,  # Music Room 4
)
MUSIC_VOICE_CHANNEL_NAMES = parse_channel_names(
    os.getenv("MUSIC_VOICE_CHANNEL_NAMES"),
    tuple(f"Music Room {index}" for index in range(1, 5)),
)


def is_music_request_channel(channel) -> bool:
    if channel is None or not isinstance(channel, discord.TextChannel):
        return False

    if MUSIC_REQUEST_CHANNEL_IDS:
        return channel.id in MUSIC_REQUEST_CHANNEL_IDS

    channel_name = normalized_channel_name(channel.name)
    return any(
        normalized_channel_name(allowed_name) in channel_name
        for allowed_name in MUSIC_REQUEST_CHANNEL_NAMES
    )


def is_allowed_music_voice_channel(channel) -> bool:
    if channel is None or not isinstance(channel, discord.VoiceChannel):
        return False

    if MUSIC_VOICE_CHANNEL_IDS:
        return channel.id in MUSIC_VOICE_CHANNEL_IDS

    channel_name = normalized_channel_name(channel.name)
    return channel_name in {
        normalized_channel_name(allowed_name)
        for allowed_name in MUSIC_VOICE_CHANNEL_NAMES
    }


def find_music_request_channel(guild: discord.Guild | None):
    if guild is None:
        return None
    return next(
        (channel for channel in guild.text_channels if is_music_request_channel(channel)),
        None,
    )

# ==================== 0. ระบบดึงข้อมูลเพลงจาก Spotify (ไม่ใช้ Official API) ====================
# ใช้หน้า Embed สาธารณะของ Spotify (เหมือนที่เว็บอื่นใช้ฝัง preview เพลง)
# ข้อดี: ไม่ต้องมี Client ID/Secret และไม่ต้องมีบัญชี Spotify Premium เลย
# ข้อควรรู้: เป็นการอ่านโครงสร้างหน้าเว็บสาธารณะ ถ้า Spotify เปลี่ยนโครงสร้างหน้าเว็บ อาจต้องแก้โค้ดส่วนนี้ใหม่


def parse_spotify_url(url: str):
    """แยกประเภท (track/album/playlist) และ ID จากลิงก์ Spotify"""
    for kind in ("track", "album", "playlist"):
        marker = f"/{kind}/"
        if marker in url:
            spotify_id = url.split(marker)[1].split("?")[0].split("/")[0]
            return kind, spotify_id
    return None, None


def parse_deezer_url(url: str):
    """แยกประเภท (track/album/playlist) และ ID จากลิงก์ Deezer"""
    match = re.search(r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist)/(\d+)", url)
    if match:
        return match.group(1), match.group(2)
    return None, None


def parse_apple_music_url(url: str):
    """แยกประเภท (song/album/playlist) และ ID จากลิงก์ Apple Music"""
    parsed = urlparse(url)
    if "music.apple.com" not in parsed.netloc:
        return None, None, None

    match = re.search(r"/(?:[a-z]{2}/)?(album|song|playlist)/[^/]+/([^/?#]+)", parsed.path)
    if not match:
        return None, None, None

    kind = match.group(1)
    apple_id = match.group(2)
    query = parse_qs(parsed.query)
    track_id = query.get("i", [None])[0]
    return kind, apple_id, track_id


def apple_track_to_info(track: dict):
    duration = track.get("trackTimeMillis")
    return {
        "title": track.get("trackName", ""),
        "artist": track.get("artistName", ""),
        "duration": int(duration / 1000) if duration else None,
    }


def parse_iso8601_duration(duration: str | None):
    if not duration:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        duration,
    )
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def apple_schema_track_to_info(track: dict, fallback_artist: str = ""):
    artist = track.get("byArtist") or track.get("creator") or fallback_artist
    if isinstance(artist, dict):
        artist = artist.get("name", "")
    elif isinstance(artist, list):
        artist = ", ".join(item.get("name", "") for item in artist if isinstance(item, dict))

    return {
        "title": track.get("name", ""),
        "artist": artist or fallback_artist,
        "duration": parse_iso8601_duration(track.get("duration")),
    }


async def fetch_apple_music_page_track_names(session: aiohttp.ClientSession, url: str):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PorkHyunMusicBot/1.0)"}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            raise RuntimeError(f"เปิดหน้า Apple Music ไม่ได้ (status {r.status})")
        html = await r.text()

    matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>\s*(.*?)\s*</script>',
        html,
        re.DOTALL,
    )

    for raw_json in matches:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        fallback_artist = ""
        by_artist = data.get("byArtist") or data.get("creator")
        if isinstance(by_artist, dict):
            fallback_artist = by_artist.get("name", "")
        elif isinstance(by_artist, list):
            fallback_artist = ", ".join(item.get("name", "") for item in by_artist if isinstance(item, dict))
        elif isinstance(by_artist, str):
            fallback_artist = by_artist

        tracks = data.get("tracks") or data.get("track") or []
        if isinstance(tracks, dict):
            tracks = tracks.get("itemListElement") or [tracks]

        normalized_tracks = []
        for track in tracks:
            if not isinstance(track, dict):
                continue
            normalized_tracks.append(track.get("item") if isinstance(track.get("item"), dict) else track)

        track_infos = [
            apple_schema_track_to_info(track, fallback_artist)
            for track in normalized_tracks
            if isinstance(track, dict)
        ]
        track_infos = [track for track in track_infos if track.get("title")]
        if track_infos:
            return track_infos

    return []


async def fetch_apple_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url) as r:
        if r.status != 200:
            raise RuntimeError(f"เปิด Apple Music lookup ไม่ได้ (status {r.status})")
        return await r.json(content_type=None)


async def fetch_apple_music_track_names(
    session: aiohttp.ClientSession,
    kind: str,
    apple_id: str,
    track_id: str | None = None,
    source_url: str | None = None,
):
    if kind == "playlist":
        return await fetch_apple_music_page_track_names(session, source_url) if source_url else []

    lookup_id = track_id if track_id else apple_id
    if kind == "song" or track_id:
        url = f"https://itunes.apple.com/lookup?id={quote(lookup_id, safe='')}&country=TH"
    else:
        url = f"https://itunes.apple.com/lookup?id={quote(lookup_id, safe='')}&entity=song&country=TH"

    data = await fetch_apple_json(session, url)
    results = data.get("results", [])
    tracks = [
        apple_track_to_info(item)
        for item in results
        if item.get("wrapperType") == "track" and item.get("kind") == "song"
    ]
    tracks = [track for track in tracks if track.get("title")]
    if tracks:
        return tracks

    if source_url and kind in ("album", "playlist"):
        return await fetch_apple_music_page_track_names(session, source_url)

    return []


async def search_apple_music_track(session: aiohttp.ClientSession, search: str):
    url = (
        "https://itunes.apple.com/search"
        f"?term={quote(search, safe='')}&media=music&entity=song&country=TH&limit=1"
    )
    data = await fetch_apple_json(session, url)
    results = data.get("results", [])
    if not results:
        return None
    return apple_track_to_info(results[0])


def deezer_track_to_info(track: dict):
    artist = track.get("artist") or {}
    return {
        "title": track.get("title", ""),
        "artist": artist.get("name", ""),
        "duration": track.get("duration"),
    }


async def fetch_deezer_json(session: aiohttp.ClientSession, endpoint: str):
    api_url = f"https://api.deezer.com/{endpoint}"
    async with session.get(api_url) as r:
        if r.status != 200:
            raise RuntimeError(f"เปิด Deezer API ไม่ได้ (status {r.status})")
        data = await r.json()

    if isinstance(data, dict) and data.get("error"):
        message = data["error"].get("message", "unknown error")
        raise RuntimeError(f"Deezer API error: {message}")

    return data


async def fetch_deezer_track_names(session: aiohttp.ClientSession, kind: str, deezer_id: str):
    if kind == "track":
        track = await fetch_deezer_json(session, f"track/{deezer_id}")
        return [deezer_track_to_info(track)] if track.get("title") else []

    data = await fetch_deezer_json(session, f"{kind}/{deezer_id}")
    tracks = data.get("tracks", {}).get("data", [])
    return [deezer_track_to_info(track) for track in tracks if track.get("title")]


async def search_deezer_track(session: aiohttp.ClientSession, search: str):
    data = await fetch_deezer_json(session, f"search/track?q={quote(search, safe='')}&limit=1")
    tracks = data.get("data", [])
    if not tracks:
        return None
    return deezer_track_to_info(tracks[0])


async def fetch_spotify_track_names(session: aiohttp.ClientSession, kind: str, spotify_id: str):
    """
    ดึงรายชื่อเพลง (ชื่อเพลง + ศิลปิน) จากหน้า Embed สาธารณะของ Spotify
    คืนค่าเป็น list ของ string เช่น ["Song Name Artist Name", ...]
    """
    embed_url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PorkHyunMusicBot/1.0)"}

    async with session.get(embed_url, headers=headers) as r:
        if r.status != 200:
            raise RuntimeError(f"เปิดหน้า Spotify embed ไม่ได้ (status {r.status})")
        html = await r.text()

    # Spotify ฝังข้อมูลเพลงไว้เป็น JSON ใน <script id="__NEXT_DATA__">...</script>
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not match:
        raise RuntimeError("ดึงข้อมูลจากหน้า Spotify ไม่สำเร็จ (โครงสร้างหน้าเว็บอาจเปลี่ยนไป ลองแจ้งแอดมินให้แก้โค้ด)")

    try:
        data = json.loads(match.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"อ่านข้อมูลเพลงจาก Spotify ไม่สำเร็จ: {e}")

    spotify_tracks = []

    if kind == "track":
        name = entity.get("title", "")
        artist = entity.get("subtitle", "")
        if name:
            spotify_tracks.append({"title": name, "artist": artist})
    else:
        # album / playlist -> มีรายการเพลงอยู่ใน trackList
        for item in entity.get("trackList", []):
            name = item.get("title", "")
            artist = item.get("subtitle", "")
            if name:
                spotify_tracks.append({"title": name, "artist": artist})

    return spotify_tracks


def normalize_track_text(text: str) -> str:
    """ทำให้ข้อความเทียบกันง่ายขึ้นตอนจับคู่ Spotify -> YouTube"""
    text = text.lower()
    text = re.sub(r"[\(\)\[\]\{\}\-_/|:;,.!?\"']", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def lavalink_search_queries(track_info: dict):
    title = track_info.get("title", "").strip()
    artist = track_info.get("artist", "").strip()
    base = f"{title} {artist}".strip()

    queries = [
        f'ytsearch:"{title}" "{artist}" official audio',
        f"ytsearch:{base} official audio",
        f"ytsearch:{base}",
    ]

    # กันกรณี subtitle จาก Spotify มีคำเสริมแปลก ๆ หรือหลายศิลปินมากเกินไป
    first_artist = re.split(r",|&| feat\. | ft\. ", artist, flags=re.IGNORECASE)[0].strip()
    if first_artist and first_artist != artist:
        queries.append(f"ytsearch:{title} {first_artist} official audio")

    return queries


def score_youtube_track(candidate, track_info: dict) -> int:
    spotify_title = track_info.get("title", "")
    spotify_artist = track_info.get("artist", "")
    wanted_title = normalize_track_text(spotify_title)
    wanted_artist = normalize_track_text(spotify_artist)
    candidate_title = normalize_track_text(getattr(candidate, "title", ""))
    candidate_author = normalize_track_text(getattr(candidate, "author", ""))
    combined = f"{candidate_title} {candidate_author}"

    score = 0
    if wanted_title and wanted_title in candidate_title:
        score += 45

    title_words = [word for word in wanted_title.split() if len(word) > 1]
    if title_words:
        matched_words = sum(1 for word in title_words if word in candidate_title)
        score += int((matched_words / len(title_words)) * 25)

    artist_parts = [
        part.strip()
        for part in re.split(r",|&| feat\. | ft\. ", wanted_artist, flags=re.IGNORECASE)
        if part.strip()
    ]
    if artist_parts:
        if any(part in combined for part in artist_parts):
            score += 35
        else:
            score -= 35

    unwanted_versions = (
        "remix",
        "mix",
        "cover",
        "karaoke",
        "instrumental",
        "live",
        "sped up",
        "slowed",
        "nightcore",
        "dstrd",
        "sgnl",
    )
    spotify_all = normalize_track_text(f"{spotify_title} {spotify_artist}")
    for word in unwanted_versions:
        if word in combined and word not in spotify_all:
            score -= 18

    if "official" in combined or "audio" in combined:
        score += 8

    wanted_duration = track_info.get("duration")
    candidate_duration_ms = (
        getattr(candidate, "length", None)
        or getattr(candidate, "duration", None)
    )
    if wanted_duration and candidate_duration_ms:
        candidate_duration = int(candidate_duration_ms / 1000)
        diff = abs(candidate_duration - int(wanted_duration))
        if diff <= 3:
            score += 20
        elif diff <= 8:
            score += 12
        elif diff <= 20:
            score += 4
        else:
            score -= 10

    return score


async def find_best_youtube_match(track_info: dict):
    best_track = None
    best_score = -999
    seen = set()

    for query in lavalink_search_queries(track_info):
        results = await wavelink.Playable.search(query)
        if not results:
            continue

        for candidate in results[:8]:
            key = (
                getattr(candidate, "uri", None)
                or f"{getattr(candidate, 'title', '')}:{getattr(candidate, 'author', '')}"
            )
            if key in seen:
                continue
            seen.add(key)

            score = score_youtube_track(candidate, track_info)
            if score > best_score:
                best_track = candidate
                best_score = score

    # ถ้าคะแนนต่ำมาก แปลว่าศิลปินไม่ตรงหรือเป็นคนละเวอร์ชัน ให้ข้ามดีกว่าใส่เพลงผิดคิว
    if best_score < 35:
        return None

    return best_track


async def queue_track_infos(vc: wavelink.Player, track_infos: list[dict]):
    added_count = 0
    first_track_title = None

    for track_info in track_infos:
        track = await find_best_youtube_match(track_info)
        if not track:
            continue

        await vc.queue.put_wait(track)
        added_count += 1
        if first_track_title is None:
            first_track_title = track.title

    return added_count, first_track_title


def format_duration(track) -> str:
    duration_ms = getattr(track, "length", None) or getattr(track, "duration", None)
    if not duration_ms:
        return "--:--"
    total_seconds = int(duration_ms / 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def trim_discord_label(text: str, limit: int = 100) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def track_identity(track) -> str | None:
    if not track:
        return None

    for attr in ("identifier", "uri", "encoded"):
        value = getattr(track, attr, None)
        if value:
            return str(value)

    title = getattr(track, "title", None)
    author = getattr(track, "author", None)
    duration = getattr(track, "length", None) or getattr(track, "duration", None)
    if title:
        return f"{title}|{author}|{duration}"

    return None


def mark_manual_queue_jump(player, selected_track):
    player._manual_queue_jump_until = time.time() + 5
    player._manual_queue_jump_previous_key = track_identity(getattr(player, "current", None))
    player._manual_queue_jump_target_key = track_identity(selected_track)


def should_ignore_manual_queue_jump_end(player, ended_track) -> bool:
    until = getattr(player, "_manual_queue_jump_until", 0)
    if time.time() > until:
        for attr in (
            "_manual_queue_jump_until",
            "_manual_queue_jump_previous_key",
            "_manual_queue_jump_target_key",
        ):
            if hasattr(player, attr):
                delattr(player, attr)
        return False

    ended_key = track_identity(ended_track)
    previous_key = getattr(player, "_manual_queue_jump_previous_key", None)
    target_key = getattr(player, "_manual_queue_jump_target_key", None)

    if ended_key and target_key and ended_key == target_key:
        return False

    if not ended_key or not previous_key or ended_key == previous_key:
        print("↪️ ข้าม track_end ของเพลงเก่าหลังเลือกเพลงจากคิว")
        return True

    return False


class TrackSelect(discord.ui.Select):
    def __init__(self, parent: "TrackSearchView"):
        self.parent_view = parent
        start = parent.page * parent.page_size
        end = min(start + parent.page_size, len(parent.tracks))
        options = []

        for index in range(start, end):
            track = parent.tracks[index]
            label = trim_discord_label(f"{index + 1}. {track.title}", 100)
            description = trim_discord_label(
                f"{format_duration(track)} • {getattr(track, 'author', 'Unknown')}",
                100,
            )
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(index),
                )
            )

        super().__init__(
            placeholder="เลือกเพลง",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.select_track(interaction, int(self.values[0]))


class TrackSearchView(discord.ui.View):
    def __init__(self, ctx: commands.Context, vc: wavelink.Player, tracks: list, query: str):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.vc = vc
        self.tracks = tracks[:20]
        self.query = query
        self.page = 0
        self.page_size = 10
        self.message = None
        self.refresh_items()

    @property
    def max_page(self) -> int:
        return max(0, (len(self.tracks) - 1) // self.page_size)

    def refresh_items(self):
        self.clear_items()
        self.add_item(TrackSelect(self))
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.max_page
        self.first_page.disabled = self.page == 0
        self.last_page.disabled = self.page >= self.max_page
        self.add_item(self.first_page)
        self.add_item(self.prev_page)
        self.add_item(self.next_page)
        self.add_item(self.last_page)
        self.add_item(self.cancel)

    def make_embed(self) -> discord.Embed:
        start = self.page * self.page_size
        end = min(start + self.page_size, len(self.tracks))
        lines = []

        for index in range(start, end):
            track = self.tracks[index]
            title = trim_discord_label(track.title, 70)
            author = trim_discord_label(getattr(track, "author", "Unknown"), 40)
            lines.append(f"{index + 1}. `[{format_duration(track)}]` **{title}** by **{author}**")

        embed = discord.Embed(
            title=f"ผลการค้นหา: {trim_discord_label(self.query, 80)}",
            description="\n".join(lines),
            color=0x2b2135,
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} | เลือกเพลงจากเมนูด้านล่าง")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("เมนูนี้เป็นของคนที่สั่งค้นหาเพลงนะ", ephemeral=True)
            return False
        return True

    async def select_track(self, interaction: discord.Interaction, index: int):
        track = self.tracks[index]
        await self.vc.queue.put_wait(track)

        if not self.vc.playing:
            next_track = self.vc.queue.get()
            await self.vc.play(next_track)

        write_player_status(self.vc)

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=f"🎵 เพิ่มเข้าคิวเรียบร้อย: **{track.title}**",
            embed=None,
            view=self,
        )
        self.stop()

    async def update_page(self, interaction: discord.Interaction):
        self.refresh_items()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary, row=1)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await self.update_page(interaction)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_page(interaction)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_page, self.page + 1)
        await self.update_page(interaction)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary, row=1)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.max_page
        await self.update_page(interaction)

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="ยกเลิกการเลือกเพลงแล้ว", embed=None, view=self)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="หมดเวลาเลือกเพลงแล้ว", embed=None, view=self)
            except discord.HTTPException:
                pass


class QueueSelect(discord.ui.Select):
    def __init__(self, parent: "QueueControlView"):
        self.parent_view = parent
        options = []

        if parent.current:
            options.append(
                discord.SelectOption(
                    label=trim_discord_label(f"กำลังเล่น: {parent.current.title}", 100),
                    description=trim_discord_label(f"{format_duration(parent.current)} • เล่นอยู่ตอนนี้", 100),
                    value="current",
                )
            )

        start = parent.page * parent.page_size
        end = min(start + parent.page_size, len(parent.queue_tracks))
        for index in range(start, end):
            track = parent.queue_tracks[index]
            options.append(
                discord.SelectOption(
                    label=trim_discord_label(f"{index + 1}. {track.title}", 100),
                    description=trim_discord_label(
                        f"{format_duration(track)} • {getattr(track, 'author', 'Unknown')}",
                        100,
                    ),
                    value=str(index),
                )
            )

        if not options:
            options.append(
                discord.SelectOption(
                    label="ไม่มีเพลงในคิว",
                    description="เพิ่มเพลงก่อนแล้วค่อยกลับมาดูคิว",
                    value="empty",
                )
            )

        super().__init__(
            placeholder="เลือกเพลงที่จะเปลี่ยนไปเล่น",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
            disabled=not parent.queue_tracks and not parent.current,
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value in ("empty", "current"):
            await interaction.response.defer()
            return
        await self.parent_view.jump_to_track(interaction, int(value))


class QueueControlView(discord.ui.View):
    def __init__(self, vc: wavelink.Player):
        super().__init__(timeout=60)
        self.vc = vc
        self.current = vc.current if vc and vc.playing else None
        self.queue_tracks = list(vc.queue) if vc and not vc.queue.is_empty else []
        self.page = 0
        self.page_size = 10
        self.message = None
        self.refresh_items()

    @property
    def max_page(self) -> int:
        return max(0, (len(self.queue_tracks) - 1) // self.page_size)

    def refresh_items(self):
        self.clear_items()
        self.add_item(QueueSelect(self))
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.max_page
        self.add_item(self.prev_page)
        self.add_item(self.next_page)
        self.add_item(self.refresh)

    def make_embed(self) -> discord.Embed:
        embed = discord.Embed(title="📋 คิวเพลง", color=0x2b2135)

        if self.current:
            embed.add_field(
                name="กำลังเล่น",
                value=f"`[{format_duration(self.current)}]` **{trim_discord_label(self.current.title, 80)}**",
                inline=False,
            )
        else:
            embed.add_field(name="กำลังเล่น", value="ไม่มีเพลงที่กำลังเล่นอยู่", inline=False)

        if self.queue_tracks:
            start = self.page * self.page_size
            end = min(start + self.page_size, len(self.queue_tracks))
            lines = []
            for index in range(start, end):
                track = self.queue_tracks[index]
                lines.append(
                    f"{index + 1}. `[{format_duration(track)}]` **{trim_discord_label(track.title, 70)}**"
                )
            embed.add_field(
                name=f"รอคิวอยู่ ({len(self.queue_tracks)} เพลง)",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(name="รอคิวอยู่", value="คิวว่าง", inline=False)

        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} | เลือกเพลงเพื่อเปลี่ยนไปเล่นทันที")
        return embed

    async def jump_to_track(self, interaction: discord.Interaction, index: int):
        if index >= len(self.queue_tracks):
            return await interaction.response.send_message("เพลงนี้ไม่อยู่ในคิวแล้ว ลองกดรีเฟรช", ephemeral=True)

        selected = self.queue_tracks[index]
        remaining = self.queue_tracks[index + 1 :]

        mark_manual_queue_jump(self.vc, selected)
        self.vc.queue.clear()
        for track in remaining:
            await self.vc.queue.put_wait(track)

        await self.vc.play(selected)
        self.current = selected
        self.queue_tracks = remaining
        self.page = min(self.page, self.max_page)
        self.refresh_items()
        await interaction.response.edit_message(
            content=f"🎵 เปลี่ยนไปเล่น: **{selected.title}**",
            embed=self.make_embed(),
            view=self,
        )

    async def update_page(self, interaction: discord.Interaction):
        self.refresh_items()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_page(interaction)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_page, self.page + 1)
        await self.update_page(interaction)

    @discord.ui.button(label="รีเฟรช", style=discord.ButtonStyle.primary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = self.vc.current if self.vc and self.vc.playing else None
        self.queue_tracks = list(self.vc.queue) if self.vc and not self.vc.queue.is_empty else []
        self.page = min(self.page, self.max_page)
        await self.update_page(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ==================== 0.5 ระบบ Dashboard กลาง (รวมสถานะทุกบอทไว้ที่เดียว) ====================
# แนวคิด: แต่ละบอท (1-4) เขียนสถานะของตัวเอง (ห้อง/เพลงที่เล่น) ลงไฟล์ JSON แยกกัน
# บอทตัวที่ 1 เท่านั้นเป็นเจ้าของข้อความ Dashboard และคอยอ่านไฟล์ทั้งหมดมารวมแสดงผลทุก 5 วินาที
# (ต้องเป็นบอทตัวเดียวเพราะ Discord ให้แก้ไขข้อความได้เฉพาะบอทที่เป็นคนโพสต์เท่านั้น)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.path.join(BASE_DIR, "status")
os.makedirs(STATUS_DIR, exist_ok=True)
DASHBOARD_STATE_PATH = os.path.join(BASE_DIR, "dashboard_state.json")
STATUS_STALE_SECONDS = 120  # ถ้าไฟล์สถานะของบอทไหนไม่อัปเดตเกินนี้ ถือว่าบอทนั้นน่าจะออฟไลน์/ค้าง
PLAYBACK_STATUS_HEARTBEAT_SECONDS = 30  # refresh ไฟล์สถานะระหว่างเพลงยาว โดยไม่ edit dashboard ถ้าเพลงยังเดิม
QUEUE_REQUEST_POLL_SECONDS = 2
EMPTY_VOICE_CHECK_SECONDS = 15
EMPTY_VOICE_GRACE_SECONDS = 60


def queue_snapshot_from_player(player) -> list[dict]:
    queue = getattr(player, "queue", None)
    if not queue or queue.is_empty:
        return []

    tracks = []
    for track in list(queue):
        tracks.append({
            "title": getattr(track, "title", "Unknown"),
            "duration": format_duration(track),
        })
    return tracks


def write_bot_status(channel_name: str | None, track_title: str | None, queue_tracks: list[dict] | None = None):
    """????????????????????????????????????????? ???????????? 1 ???????????????"""
    status = {
        "bot_index": RUNTIME_ARGS.bot_index,
        "channel_name": channel_name,
        "track_title": track_title,
        "queue": queue_tracks or [],
        "updated_at": time.time(),
    }
    path = os.path.join(STATUS_DIR, f"bot_{RUNTIME_ARGS.bot_index}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False)
    except OSError as e:
        print(f"?? ???????????????????????: {e}")


def write_player_status(player):
    current = getattr(player, "current", None)
    is_active = getattr(player, "playing", False) or getattr(player, "paused", False)
    if current and is_active:
        channel_name = getattr(getattr(player, "channel", None), "name", None)
        write_bot_status(
            channel_name=channel_name,
            track_title=current.title,
            queue_tracks=queue_snapshot_from_player(player),
        )
        return

    write_bot_status(channel_name=None, track_title=None)


def refresh_current_playback_status():
    """?????????????????????????? ????????? snapshot ???????????????"""
    for voice_client in bot.voice_clients:
        current = getattr(voice_client, "current", None)
        is_active = getattr(voice_client, "playing", False) or getattr(voice_client, "paused", False)
        if current and is_active:
            write_player_status(voice_client)
            return

    write_bot_status(channel_name=None, track_title=None)


def queue_request_path(bot_index: int) -> str:
    return os.path.join(STATUS_DIR, f"queue_request_{bot_index}.json")


def write_queue_jump_request(bot_index: int, index: int, requester_id: int):
    request = {
        "action": "jump",
        "index": index,
        "requester_id": str(requester_id),
        "created_at": time.time(),
    }
    with open(queue_request_path(bot_index), "w", encoding="utf-8") as f:
        json.dump(request, f)


async def process_queue_control_requests():
    path = queue_request_path(RUNTIME_ARGS.bot_index)
    if not os.path.exists(path):
        return

    try:
        with open(path, encoding="utf-8") as f:
            request = json.load(f)
    except (OSError, json.JSONDecodeError):
        request = None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if not request or request.get("action") != "jump":
        return
    if time.time() - request.get("created_at", 0) > 60:
        return

    player = None
    for voice_client in bot.voice_clients:
        if getattr(voice_client, "current", None):
            player = voice_client
            break
    if not player:
        return

    queue = getattr(player, "queue", None)
    if not queue or queue.is_empty:
        return

    queue_tracks = list(queue)
    try:
        index = int(request.get("index"))
    except (TypeError, ValueError):
        return

    if index < 0 or index >= len(queue_tracks):
        return

    selected = queue_tracks[index]
    remaining = queue_tracks[index + 1:]
    mark_manual_queue_jump(player, selected)
    player.queue.clear()
    for track in remaining:
        await player.queue.put_wait(track)

    await player.play(selected)
    write_player_status(player)


def human_members_in_voice_channel(channel) -> list[discord.Member]:
    return [
        member for member in getattr(channel, "members", [])
        if not getattr(member, "bot", False)
    ]


async def cleanup_empty_voice_channels():
    now = time.time()
    active_keys = set()

    for player in list(bot.voice_clients):
        channel = getattr(player, "channel", None)
        guild = getattr(player, "guild", None) or getattr(channel, "guild", None)
        if not channel or not guild:
            continue

        key = guild.id
        active_keys.add(key)
        humans = human_members_in_voice_channel(channel)
        if humans:
            bot.empty_voice_since.pop(key, None)
            continue

        empty_since = bot.empty_voice_since.setdefault(key, now)
        if now - empty_since < EMPTY_VOICE_GRACE_SECONDS:
            continue

        await disconnect_empty_voice_player(player, channel, now - empty_since)
        bot.empty_voice_since.pop(key, None)

    for key in list(bot.empty_voice_since):
        if key not in active_keys:
            bot.empty_voice_since.pop(key, None)


async def disconnect_empty_voice_player(player, channel, empty_seconds: float):
    queue = getattr(player, "queue", None)
    if queue:
        queue.clear()

    try:
        await player.disconnect()
    except Exception as e:
        print(f"⚠️ ออกจากห้องว่างไม่สำเร็จ: {e}")
        return

    write_bot_status(channel_name=None, track_title=None)
    print(
        f"🚪 ห้อง {getattr(channel, 'name', 'Unknown')} ไม่มีผู้ฟัง "
        f"{int(empty_seconds)} วิ ล้างคิวและออกจากห้องแล้ว"
    )


def save_dashboard_state(channel_id: int, message_id: int):
    try:
        with open(DASHBOARD_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"channel_id": channel_id, "message_id": message_id}, f)
    except OSError as e:
        print(f"⚠️ บันทึก dashboard_state ไม่สำเร็จ: {e}")


def load_dashboard_state():
    if not os.path.exists(DASHBOARD_STATE_PATH):
        return None
    try:
        with open(DASHBOARD_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def build_shared_dashboard_embed() -> discord.Embed:
    """Updates when the song changes (1..bot_count) ?????????????? Dashboard ?????"""
    now = time.time()
    lines = []
    border = "+----------------------------------------+"

    def box_line(text: str) -> str:
        content = trim_discord_label(text, 38)
        return f"| {content.ljust(38)} |"

    for idx in range(1, bot.bot_count + 1):
        if lines:
            lines.append(border)

        path = os.path.join(STATUS_DIR, f"bot_{idx}.json")
        status = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    status = json.load(f)
            except (OSError, json.JSONDecodeError):
                status = None

        is_fresh = status is not None and (now - status.get("updated_at", 0)) < STATUS_STALE_SECONDS

        if is_fresh and status.get("track_title"):
            channel_name = trim_discord_label(status.get("channel_name") or "Unknown", 20)
            track_title = trim_discord_label(status["track_title"], 32)
            lines.append(box_line(f"ROOM: {channel_name} | BOT {idx}"))
            lines.append(box_line(f"LISTEN: {track_title}"))
        else:
            lines.append(box_line(f"ROOM: - | BOT {idx} | IDLE"))

    status_box = "\n".join([
        border,
        *lines,
        border,
    ])

    embed = discord.Embed(
        description=f"```text\n{status_box}\n```",
        color=0xffa500,
    )
    embed.set_author(
        name=dashboard_author_name(),
        icon_url=dashboard_author_icon_url(),
    )
    embed.set_image(url="https://media4.giphy.com/media/v1.Y2lkPTc5MGI3NjExdDRwaDNra2l5ZXhwOXB3Mzlta3pyMTdkZzlwc2p1YjMxOHBjMnM4YSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/30MxopxLs9wbpzKIo6/giphy.gif")
    embed.set_footer(text="Updates when the song changes")
    return embed


def load_bot_status(bot_index: int):
    path = os.path.join(STATUS_DIR, f"bot_{bot_index}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def current_queue_snapshot(bot_index: int):
    status = load_bot_status(bot_index)
    now = time.time()
    is_fresh = status is not None and (now - status.get("updated_at", 0)) < STATUS_STALE_SECONDS
    if not is_fresh:
        return None, []
    return status, status.get("queue") or []


def build_queue_snapshot_embed(bot_index: int, voice_channel_name: str) -> discord.Embed:
    status, queue_tracks = current_queue_snapshot(bot_index)

    embed = discord.Embed(title="Queue", color=0x2b2135)
    embed.set_footer(text=f"Showing queue from bot {bot_index} for {voice_channel_name}")

    if not status or not status.get("track_title"):
        embed.description = "No active music bot was found for your voice room."
        return embed

    embed.add_field(
        name="Now Playing",
        value=f"**{status.get('track_title', 'Unknown')}**",
        inline=False,
    )

    if not queue_tracks:
        embed.add_field(name="Up Next", value="No songs waiting in queue.", inline=False)
        return embed

    lines = []
    for index, item in enumerate(queue_tracks[:10], start=1):
        duration = item.get("duration") or "--:--"
        title = item.get("title") or "Unknown"
        lines.append(f"`{index}.` `[{duration}]` **{trim_discord_label(title, 80)}**")

    remaining = len(queue_tracks) - 10
    if remaining > 0:
        lines.append(f"and {remaining} more songs...")

    embed.add_field(name=f"Up Next ({len(queue_tracks)} songs)", value="\n".join(lines), inline=False)
    return embed


async def defer_ephemeral_interaction(interaction: discord.Interaction, action: str) -> bool:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        return True
    except discord.NotFound:
        print(f"⚠️ interaction หมดอายุแล้วก่อนตอบ: {action}")
    except discord.HTTPException as e:
        print(f"⚠️ defer interaction ไม่สำเร็จ ({action}): {e}")
    return False


async def send_ephemeral_followup(interaction: discord.Interaction, action: str, content: str | None = None, **kwargs):
    try:
        await interaction.followup.send(content=content, ephemeral=True, **kwargs)
    except discord.NotFound:
        print(f"⚠️ interaction followup หมดอายุแล้ว: {action}")
    except discord.HTTPException as e:
        print(f"⚠️ ส่ง interaction followup ไม่สำเร็จ ({action}): {e}")


async def safe_interaction_edit_message(interaction: discord.Interaction, action: str, **kwargs):
    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        else:
            await interaction.edit_original_response(**kwargs)
    except discord.NotFound:
        print(f"⚠️ interaction edit หมดอายุแล้ว: {action}")
    except discord.HTTPException as e:
        print(f"⚠️ แก้ interaction message ไม่สำเร็จ ({action}): {e}")


class SnapshotQueueSelect(discord.ui.Select):
    def __init__(self, parent: "SnapshotQueueView"):
        self.parent_view = parent
        _status, queue_tracks = current_queue_snapshot(parent.bot_index)
        options = []
        for index, item in enumerate(queue_tracks[:25]):
            duration = item.get("duration") or "--:--"
            title = item.get("title") or "Unknown"
            options.append(
                discord.SelectOption(
                    label=trim_discord_label(f"{index + 1}. {title}", 100),
                    description=trim_discord_label(f"Play now - {duration}", 100),
                    value=str(index),
                )
            )

        super().__init__(
            placeholder="Choose a queued song to play now",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.request_jump(interaction, int(self.values[0]))


class SnapshotQueueView(discord.ui.View):
    def __init__(self, bot_index: int, voice_channel_name: str):
        super().__init__(timeout=60)
        self.bot_index = bot_index
        self.voice_channel_name = voice_channel_name
        _status, queue_tracks = current_queue_snapshot(bot_index)
        if queue_tracks:
            self.add_item(SnapshotQueueSelect(self))

    async def request_jump(self, interaction: discord.Interaction, index: int):
        write_queue_jump_request(self.bot_index, index, interaction.user.id)
        await safe_interaction_edit_message(
            interaction,
            "queue jump",
            content=f"Switch request sent to bot {self.bot_index}. Press Refresh in a moment.",
            embed=build_queue_snapshot_embed(self.bot_index, self.voice_channel_name),
            view=SnapshotQueueView(self.bot_index, self.voice_channel_name),
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_interaction_edit_message(
            interaction,
            "queue refresh",
            content=None,
            embed=build_queue_snapshot_embed(self.bot_index, self.voice_channel_name),
            view=SnapshotQueueView(self.bot_index, self.voice_channel_name),
        )


# ==================== 1. คลาสปุ่มหน้าปัด (Dashboard) ====================
class MusicDashboard(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="เว็บของเรา", style=discord.ButtonStyle.link, url="https://www.khuiai.com/th/profile/Porkhyun"))

    @discord.ui.button(label="ฟังเพลงใหม่", style=discord.ButtonStyle.success, emoji="▶️", custom_id="play_btn")
    async def play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await defer_ephemeral_interaction(interaction, "dashboard play help"):
            return

        help_text = """**🎵 คู่มือคำสั่งบอทเพลง Pork Hyun Radio:**
`!p` หรือ `!play <ชื่อเพลง / ลิงก์ YouTube / ลิงก์ Spotify>` : เรียกบอทเข้าห้องเสียงเพื่อเปิดเพลง
รองรับลิงก์ Spotify / Deezer / Apple Music ทั้งเพลงเดี่ยว, อัลบั้ม และเพลย์ลิสต์
`!q` : เช็คคิวเพลง
`!skip` : สั่งข้ามเพลงที่กำลังเล่นอยู่ไปฟังเพลงถัดไป
`!pause` : สั่งหยุดเพลงชั่วคราว (พักเบรก)
`!resume` : สั่งให้เพลงที่หยุดไว้เล่นต่อจากเดิม
`!clear` หรือ `!c` : สั่งล้างคิวเพลงทั้งหมด
`!stop` : สั่งให้บอทหยุดเล่นเพลงและออกจากห้องเสียง

ℹ️ บอทจะถูกจัดสรรให้ประจำห้องเสียงอัตโนมัติ เข้าห้องไหนก็จะมีบอทของห้องนั้นมาเล่นให้"""
        await send_ephemeral_followup(interaction, "dashboard play help", help_text)

    @discord.ui.button(label="\u0e04\u0e34\u0e27\u0e40\u0e1e\u0e25\u0e07", style=discord.ButtonStyle.primary, emoji="\U0001F4CB", custom_id="queue_btn")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await defer_ephemeral_interaction(interaction, "dashboard queue"):
            return

        voice_state = getattr(interaction.user, "voice", None)
        voice_channel = getattr(voice_state, "channel", None)
        if not voice_channel:
            return await send_ephemeral_followup(interaction, "dashboard queue no voice", "Join a voice room first, then press Queue again.")

        if not is_allowed_music_voice_channel(voice_channel):
            return await send_ephemeral_followup(
                interaction,
                "dashboard queue invalid voice",
                "This feature is available only in Music Room 1-4.",
            )

        bot_index = assigned_bot_index_for_voice_channel(voice_channel)
        view = SnapshotQueueView(bot_index, voice_channel.name)
        await send_ephemeral_followup(
            interaction,
            "dashboard queue",
            embed=build_queue_snapshot_embed(bot_index, voice_channel.name),
            view=view,
        )

    @discord.ui.button(label="Donate", style=discord.ButtonStyle.success, emoji="🪙", custom_id="donate_btn")
    async def donate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await defer_ephemeral_interaction(interaction, "dashboard donate"):
            return
        await send_ephemeral_followup(interaction, "dashboard donate", "💸 ขอบคุณที่สนับสนุนครับ! สามารถโดเนทได้ที่ห้อง <#1511062155640963072> เลยครับ!")


# ==================== 2. คลาสสถาปัตยกรรมระดับเทพ ====================
class OreoCloneBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.bot_index = RUNTIME_ARGS.bot_index
        self.bot_count = int(os.getenv("MUSICBOT_COUNT", "4"))
        self.dashboard_msg = None
        self.dashboard_last_signature = None
        self.empty_voice_since = {}

    async def setup_hook(self):
        self.add_view(MusicDashboard())
        retries = int(os.getenv("LAVALINK_CONNECT_RETRIES", "30"))
        delay = int(os.getenv("LAVALINK_CONNECT_DELAY", "5"))

        for attempt in range(1, retries + 1):
            try:
                nodes = [wavelink.Node(uri="http://127.0.0.1:2333", password="youshallnotpass")]
                await wavelink.Pool.connect(client=self, nodes=nodes)
                return
            except Exception as e:
                if attempt >= retries:
                    raise
                print(f"Lavalink ยังไม่พร้อม ({attempt}/{retries}): {e}. รอ {delay} วินาที...")
                await asyncio.sleep(delay)

    async def on_ready(self):
        listen_status = discord.Activity(type=discord.ActivityType.listening, name="ฟังเพลง 24 ชม. | !play")
        await self.change_presence(status=discord.Status.online, activity=listen_status)
        print(f'✅ พระเจ้า {self.user} ประทับร่าง และพร้อมลุยแล้ว!')

        # เขียนสถานะเริ่มต้น (ว่าง) ทันทีที่บอทออนไลน์ กันไฟล์สถานะเก่าค้างจากรอบก่อน
        write_bot_status(channel_name=None, track_title=None)

        if not refresh_playback_status_task.is_running():
            refresh_playback_status_task.start()
        if not process_queue_requests_task.is_running():
            process_queue_requests_task.start()
        if not empty_voice_cleanup_task.is_running():
            empty_voice_cleanup_task.start()

        # เฉพาะบอทตัวที่ 1: กู้คืน Dashboard เดิม (ถ้ามี) แล้วเริ่ม loop อัปเดตสถานะรวม
        if self.bot_index == 1:
            state = load_dashboard_state()
            if state:
                channel = self.get_channel(state.get("channel_id"))
                if channel:
                    try:
                        self.dashboard_msg = await channel.fetch_message(state.get("message_id"))
                        await self.dashboard_msg.edit(view=MusicDashboard())
                        self.dashboard_last_signature = build_shared_dashboard_embed().description
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        self.dashboard_msg = None

            if not update_shared_dashboard_task.is_running():
                update_shared_dashboard_task.start()

bot = OreoCloneBot()


@bot.check
async def music_commands_only_in_request_channel(ctx: commands.Context) -> bool:
    if is_music_request_channel(ctx.channel):
        return True

    if bot.bot_index == 1 and ctx.guild:
        request_channel = find_music_request_channel(ctx.guild)
        destination = request_channel.mention if request_channel else "ห้องขอเพลง"
        try:
            warning = await ctx.send(f"ใช้คำสั่งเพลงได้เฉพาะใน {destination} เท่านั้นครับ")
            await warning.delete(delay=8)
        except discord.HTTPException:
            pass
    return False


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
        return
    raise error


@tasks.loop(seconds=PLAYBACK_STATUS_HEARTBEAT_SECONDS)
async def refresh_playback_status_task():
    refresh_current_playback_status()


@tasks.loop(seconds=QUEUE_REQUEST_POLL_SECONDS)
async def process_queue_requests_task():
    await process_queue_control_requests()


@tasks.loop(seconds=EMPTY_VOICE_CHECK_SECONDS)
async def empty_voice_cleanup_task():
    await cleanup_empty_voice_channels()


@tasks.loop(seconds=5)
async def update_shared_dashboard_task():
    """ทำงานเฉพาะในบอทตัวที่ 1: อ่านสถานะทุกบอทมารวมแล้วอัปเดต Dashboard เดียว"""
    if bot.bot_index != 1 or not bot.dashboard_msg:
        return
    try:
        embed = build_shared_dashboard_embed()
        signature = embed.description
        if signature == bot.dashboard_last_signature:
            return
        await bot.dashboard_msg.edit(embed=embed)
        bot.dashboard_last_signature = signature
    except discord.NotFound:
        bot.dashboard_msg = None  # ข้อความถูกลบไปแล้ว เลิกพยายามอัปเดต
    except discord.HTTPException as e:
        print(f"⚠️ อัปเดต dashboard กลางไม่สำเร็จ: {e}")


def assigned_bot_index_for_voice_channel(channel: discord.VoiceChannel) -> int | None:
    if not is_allowed_music_voice_channel(channel):
        return None

    if MUSIC_VOICE_CHANNEL_IDS:
        channel_order = MUSIC_VOICE_CHANNEL_IDS
        channel_key = channel.id
    else:
        channel_order = tuple(
            normalized_channel_name(name)
            for name in MUSIC_VOICE_CHANNEL_NAMES
        )
        channel_key = normalized_channel_name(channel.name)

    try:
        return (channel_order.index(channel_key) % bot.bot_count) + 1
    except ValueError:
        return None


def is_this_bot_assigned_to_voice_channel(channel: discord.VoiceChannel) -> bool:
    assigned_bot_index = assigned_bot_index_for_voice_channel(channel)
    return assigned_bot_index is not None and bot.bot_index == assigned_bot_index


def should_handle_music_command(ctx: commands.Context) -> bool:
    voice_state = getattr(ctx.author, "voice", None)
    voice_channel = getattr(voice_state, "channel", None)
    if not voice_channel:
        return False

    if not is_allowed_music_voice_channel(voice_channel):
        return False

    if not is_this_bot_assigned_to_voice_channel(voice_channel):
        return False

    vc = ctx.voice_client
    if vc and getattr(vc, "channel", None) and vc.channel.id != voice_channel.id:
        return False

    return True


def dashboard_author_name() -> str:
    return "Pork Hyun Music Radio"


def dashboard_author_icon_url() -> str:
    if bot.user and bot.user.display_avatar:
        return bot.user.display_avatar.url

    return "https://media2.giphy.com/media/v1.Y2lkPTc5MGI3NjExa2VyejJwZ2tmNGhzcTNoejN6ZjJzOG9pcHBxOTVzOTNwc2Fyb3k0MyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9cw/xKi7t0cYQwTLA69YYY/giphy.gif"


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"🔥 เครื่องยนต์ Lavalink เชื่อมต่อสำเร็จ! พร้อมกระแทกหูฟัง!")


# ==================== 3. คลังคำสั่ง (Music Commands) ====================

@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if not bot.user or member.id != bot.user.id or after.channel is None:
        return
    if is_allowed_music_voice_channel(after.channel):
        return

    player = discord.utils.get(bot.voice_clients, guild=member.guild)
    if not player:
        return

    if hasattr(player, "queue"):
        player.queue.clear()
    await player.disconnect()
    write_bot_status(channel_name=None, track_title=None)
    print(f"Disconnected from disallowed voice channel: {after.channel.name}")


@bot.command(name='dashboard', aliases=['setup'])
async def spawn_dashboard(ctx: commands.Context):
    # ให้บอทตัวที่ 1 เท่านั้นเป็นเจ้าของ Dashboard กลาง (บอทตัวอื่นไม่ต้องสร้างซ้ำ)
    if bot.bot_index != 1:
        return

    embed = build_shared_dashboard_embed()
    view = MusicDashboard()
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    msg = await ctx.send(embed=embed, view=view)
    bot.dashboard_msg = msg
    bot.dashboard_last_signature = embed.description
    save_dashboard_state(msg.channel.id, msg.id)

    if not update_shared_dashboard_task.is_running():
        update_shared_dashboard_task.start()


@bot.command(name='play', aliases=['p'])
async def play(ctx: commands.Context, *, search: str):
    """🎵 สั่งเปิดเพลง (รองรับ Spotify (track/album/playlist), YouTube & ค้นหาชื่อเพลง)"""
    if not ctx.author.voice:
        return await ctx.send("❌ ไก่อ่อนเอ๊ย! นายต้องเข้าไปในห้องเสียงก่อนสิ!")

    voice_channel = ctx.author.voice.channel
    if not is_allowed_music_voice_channel(voice_channel):
        if bot.bot_index == 1:
            await ctx.send("บอทเพลงเข้าได้เฉพาะ Music Room 1-4 เท่านั้นครับ")
        return

    if not is_this_bot_assigned_to_voice_channel(voice_channel):
        return

    vc: wavelink.Player = ctx.voice_client
    try:
        if not vc:
            vc = await voice_channel.connect(cls=wavelink.Player, timeout=60.0)

        # ---------------- ลิงก์ Deezer (track / album / playlist) ----------------
        if "deezer.com" in search:
            kind, deezer_id = parse_deezer_url(search)

            if not kind:
                return await ctx.send("❌ ลิงก์ Deezer นี้ไม่รองรับนะ (รองรับแค่ track / album / playlist)")

            loading_msg = await ctx.send(f"🔎 กำลังดึงข้อมูลจาก Deezer ({kind})...")

            async with aiohttp.ClientSession() as session:
                deezer_tracks = await fetch_deezer_track_names(session, kind, deezer_id)

            if not deezer_tracks:
                await loading_msg.delete()
                return await ctx.send("❌ ดึงข้อมูลจาก Deezer ไม่ได้ ลองเช็คลิงก์อีกครั้ง")

            added_count, first_track_title = await queue_track_infos(vc, deezer_tracks)
            await loading_msg.delete()

            if added_count == 0:
                return await ctx.send("❌ หาเพลงจาก Deezer บน YouTube ไม่เจอเลยสักเพลง")

            if kind == "track":
                await ctx.send(f"🎵 เพิ่มเข้าคิวจาก Deezer เรียบร้อย: **{first_track_title}**")
            else:
                await ctx.send(f"📋 จัดคิวจาก Deezer {kind} สำเร็จ **{added_count}/{len(deezer_tracks)}** เพลง")

        # ---------------- ลิงก์ Apple Music (song / album / playlist) ----------------
        elif "music.apple.com" in search:
            kind, apple_id, track_id = parse_apple_music_url(search)

            if not kind:
                return await ctx.send("❌ ลิงก์ Apple Music นี้อ่านไม่ได้ ลองเช็คลิงก์อีกครั้ง")

            loading_msg = await ctx.send(f"🔎 กำลังดึงข้อมูลจาก Apple Music ({kind})...")

            async with aiohttp.ClientSession() as session:
                apple_tracks = await fetch_apple_music_track_names(session, kind, apple_id, track_id, search)

            if not apple_tracks:
                await loading_msg.delete()
                return await ctx.send("❌ ดึงข้อมูลจาก Apple Music ไม่ได้ ลองเช็คลิงก์อีกครั้ง")

            added_count, first_track_title = await queue_track_infos(vc, apple_tracks)
            await loading_msg.delete()

            if added_count == 0:
                return await ctx.send("❌ หาเพลงจาก Apple Music บน YouTube ไม่เจอเลยสักเพลง")

            if kind == "song" or track_id:
                await ctx.send(f"🎵 เพิ่มเข้าคิวจาก Apple Music เรียบร้อย: **{first_track_title}**")
            else:
                await ctx.send(f"📋 จัดคิวจาก Apple Music {kind} สำเร็จ **{added_count}/{len(apple_tracks)}** เพลง")

        # ---------------- ลิงก์ Spotify (track / album / playlist) ----------------
        elif "spotify.com" in search:
            kind, spotify_id = parse_spotify_url(search)

            if not kind:
                return await ctx.send("❌ ลิงก์ Spotify นี้ไม่รองรับนะ (รองรับแค่ track / album / playlist)")

            loading_msg = await ctx.send(f"🔎 กำลังดึงข้อมูลจาก Spotify ({kind})...")

            async with aiohttp.ClientSession() as session:
                spotify_tracks = await fetch_spotify_track_names(session, kind, spotify_id)

            if not spotify_tracks:
                await loading_msg.delete()
                return await ctx.send("❌ ดึงข้อมูลจาก Spotify ไม่ได้ ลองเช็คลิงก์อีกครั้ง")

            added_count, first_track_title = await queue_track_infos(vc, spotify_tracks)
            await loading_msg.delete()

            if added_count == 0:
                return await ctx.send("❌ หาเพลงจาก Spotify บน YouTube ไม่เจอเลยสักเพลง")

            if kind == "track":
                await ctx.send(f"🎵 เพิ่มเข้าคิวจาก Spotify เรียบร้อย: **{first_track_title}**")
            else:
                await ctx.send(f"📋 จัดคิวจาก Spotify {kind} สำเร็จ **{added_count}/{len(spotify_tracks)}** เพลง")

        # ---------------- คำค้นทั่วไป: ใช้ Deezer เป็นหลัก แล้วค่อย fallback ไปค้นหาตรง ----------------
        else:
            if re.match(r"https?://", search, flags=re.IGNORECASE):
                tracks = await wavelink.Playable.search(search)

                if not tracks:
                    return await ctx.send("❌ หาเพลงไม่เจอ! เช็คลิ้งก์หรือชื่อเพลงใหม่ซะ")

                if isinstance(tracks, wavelink.Playlist):
                    for track in tracks.tracks:
                        await vc.queue.put_wait(track)
                    await ctx.send(f"📋 เหมา Playlist: **{tracks.name}** ({len(tracks.tracks)} เพลง) เข้าคิวเรียบร้อย!")
                else:
                    track = tracks[0]
                    await vc.queue.put_wait(track)
                    if vc.playing:
                        msg = await ctx.send(f"📋 เพิ่มเข้าคิวเรียบร้อย: **{track.title}** (คิวที่ #{vc.queue.count})")
                        await msg.delete(delay=10)
            else:
                tracks = await wavelink.Playable.search(f"ytsearch:{search}")

                if not tracks:
                    return await ctx.send("❌ หาเพลงไม่เจอ! ลองเปลี่ยนคำค้นดู")

                view = TrackSearchView(ctx, vc, list(tracks), search)
                view.message = await ctx.send(embed=view.make_embed(), view=view)
                return

        if not vc.playing:
            next_track = vc.queue.get()
            await vc.play(next_track)

        refresh_current_playback_status()

    except Exception as e:
        if e.__class__.__name__ == "ChannelTimeoutException":
            await ctx.send("❌ ต่อเข้าห้องเสียงไม่ทันใน 60 วิ ลองสั่งเพลงใหม่อีกครั้ง")
            print(f"Voice connect timeout: {e}")
            return

        await ctx.send(f"❌ ระบบขัดข้องระดับ Core: `{e}`")
        print(f"Error: {e}")


@bot.command(name='skip', aliases=['s'])
async def skip(ctx: commands.Context):
    if not should_handle_music_command(ctx):
        return

    vc: wavelink.Player = ctx.voice_client
    if not vc or not vc.playing:
        return await ctx.send("❌ ไม่มีเพลงให้ข้ามเว้ย!")
    await vc.stop()
    msg = await ctx.send("⏭️ ข้ามเพลงให้แล้ว!")
    await msg.delete(delay=5)

@bot.command(name='pause')
async def pause(ctx: commands.Context):
    if not should_handle_music_command(ctx):
        return

    vc: wavelink.Player = ctx.voice_client
    if vc and vc.playing:
        await vc.pause(True)
        msg = await ctx.send("⏸️ แช่แข็งเพลงชั่วคราว!")
        await msg.delete(delay=5)

@bot.command(name='resume', aliases=['r'])
async def resume(ctx: commands.Context):
    if not should_handle_music_command(ctx):
        return

    vc: wavelink.Player = ctx.voice_client
    if vc and vc.paused:
        await vc.pause(False)
        msg = await ctx.send("▶️ ปลดล็อค! เล่นเพลงต่อแล้ว!")
        await msg.delete(delay=5)

@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx: commands.Context):
    if not should_handle_music_command(ctx):
        return

    vc: wavelink.Player = ctx.voice_client
    if not vc:
        return await ctx.send("❌ บอทไม่ได้อยู่ในห้องเสียง!")

    if vc.queue.is_empty and not vc.playing:
        return await ctx.send("📭 คิวโล่งจั๊วะ! สั่งเพลงมาสิรออะไร!")

    embed = discord.Embed(title="📋 คิวเพลงระดับพระเจ้า", color=discord.Color.red())
    if vc.playing:
        embed.add_field(name="🎵 กำลังกระแทกหูอยู่ตอนนี้", value=vc.current.title, inline=False)

    if not vc.queue.is_empty:
        q_list = []
        for index, track in enumerate(vc.queue):
            q_list.append(f"`{index + 1}.` {track.title}")
            if index == 9:
                q_list.append(f"และวิญญาณเพลงอีก {vc.queue.count - 10} เพลงที่รอคิวอยู่...")
                break
        embed.add_field(name=f"รอคิวอยู่ ({vc.queue.count} เพลง)", value="\n".join(q_list), inline=False)

    await ctx.send(embed=embed)

@bot.command(name='clear', aliases=['c'])
async def clear_queue(ctx: commands.Context):
    if not should_handle_music_command(ctx):
        return

    vc: wavelink.Player = ctx.voice_client
    if not vc:
        return await ctx.send("❌ บอทไม่ได้อยู่ในห้องเสียง!")
    if vc.queue.is_empty:
        msg = await ctx.send("📭 คิวโล่งอยู่แล้ว จะให้ล้างอะไรอีกล่ะ!")
        return await msg.delete(delay=5)
    vc.queue.clear()
    refresh_current_playback_status()
    msg = await ctx.send("🧹 ล้างบางคิวเพลงที่รออยู่ทั้งหมดเรียบร้อยแล้ว!")
    await msg.delete(delay=5)

@bot.command(name='stop')
async def stop(ctx: commands.Context):
    if not should_handle_music_command(ctx):
        return

    vc: wavelink.Player = ctx.voice_client
    if vc:
        vc.queue.clear()
        await vc.disconnect()
        write_bot_status(channel_name=None, track_title=None)
        msg = await ctx.send("🛑 ล้างบางคิวเพลง และเตะตัวเองกลับบ้านเรียบร้อย!")
        await msg.delete(delay=5)


# ==================== 4. ระบบจัดการเมื่อเพลงเริ่ม/จบ ====================

@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    write_player_status(payload.player)

@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    if not player:
        return

    if should_ignore_manual_queue_jump_end(player, getattr(payload, "track", None)):
        return

    if not player.queue.is_empty:
        next_track = await player.queue.get_wait()
        await player.play(next_track)
    else:
        write_bot_status(channel_name=None, track_title=None)
        try:
            msg = await player.channel.send("✅ คิวหมดแล้ว! บอทขอตัวกลับสวรรค์ก่อนนะ 💨")
            await msg.delete(delay=10)
        except:
            pass
        finally:
            await player.disconnect()


def resolve_discord_token():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--bot-index", type=int, default=int(os.getenv("BOT_INDEX", "1")))
    parser.add_argument("--token-env", default=os.getenv("DISCORD_TOKEN_ENV"))
    args, _ = parser.parse_known_args()

    if args.token_env:
        token_env = args.token_env
    elif args.bot_index == 1:
        token_env = "DISCORD_TOKEN_1" if os.getenv("DISCORD_TOKEN_1") else "DISCORD_TOKEN"
    else:
        token_env = f"DISCORD_TOKEN_{args.bot_index}"

    token = os.getenv(token_env)
    if not token:
        raise RuntimeError(
            f"ไม่พบ token ใน env `{token_env}` "
            f"(ตัวที่ 1 ใช้ DISCORD_TOKEN เดิมได้, ตัวที่ 2-4 ใช้ DISCORD_TOKEN_2/3/4)"
        )

    print(f"Starting music bot with token env: {token_env}")
    return token


# ==================== 5. รันบอท ====================
start_delay = int(os.getenv("BOT_START_DELAY", "0"))
if start_delay > 0:
    print(f"รอก่อนเริ่มบอท {start_delay} วินาที...")
    time.sleep(start_delay)

bot.run(resolve_discord_token())
