import discord
from discord.ext import commands
import wavelink
import os
import time
import base64
import aiohttp
from dotenv import load_dotenv

# โหลดตัวแปรลับจาก .env 
load_dotenv()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# ==================== 0. ระบบเชื่อมต่อ Spotify API ====================
# แคช token ไว้ใช้ซ้ำ (ไม่ต้องขอใหม่ทุกครั้งที่มีคนสั่งเพลง)
_spotify_token_cache = {"token": None, "expires_at": 0}


async def get_spotify_token(session: aiohttp.ClientSession) -> str:
    """ขอ (หรือใช้ token เดิมที่แคชไว้) Access Token จาก Spotify"""
    if _spotify_token_cache["token"] and time.time() < _spotify_token_cache["expires_at"]:
        return _spotify_token_cache["token"]

    creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    async with session.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {creds}"},
        data={"grant_type": "client_credentials"},
    ) as r:
        data = await r.json()
        token = data["access_token"]
        # เผื่อเวลาหมดอายุไว้ล่วงหน้า 60 วินาที กันเคส token หมดอายุพอดีตอนใช้งาน
        _spotify_token_cache["token"] = token
        _spotify_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        return token


def parse_spotify_url(url: str):
    """แยกประเภท (track/album/playlist) และ ID จากลิงก์ Spotify"""
    for kind in ("track", "album", "playlist"):
        marker = f"/{kind}/"
        if marker in url:
            spotify_id = url.split(marker)[1].split("?")[0].split("/")[0]
            return kind, spotify_id
    return None, None


async def fetch_spotify_track_names(session: aiohttp.ClientSession, kind: str, spotify_id: str):
    """
    ดึงรายชื่อเพลง (ชื่อเพลง + ศิลปิน) จาก Spotify ตามประเภทลิงก์
    คืนค่าเป็น list ของ string เช่น ["Song Name Artist Name", ...]
    """
    token = await get_spotify_token(session)
    headers = {"Authorization": f"Bearer {token}"}
    song_queries = []

    if kind == "track":
        async with session.get(f"https://api.spotify.com/v1/tracks/{spotify_id}", headers=headers) as r:
            data = await r.json()
            song_queries.append(f"{data['name']} {data['artists'][0]['name']}")

    elif kind == "album":
        url = f"https://api.spotify.com/v1/albums/{spotify_id}/tracks?limit=50"
        while url:
            async with session.get(url, headers=headers) as r:
                data = await r.json()
                for item in data.get("items", []):
                    song_queries.append(f"{item['name']} {item['artists'][0]['name']}")
                url = data.get("next")  # Spotify ส่งลิงก์หน้าถัดไปมาให้เอง ถ้ามีเพลงเกิน 50

    elif kind == "playlist":
        url = f"https://api.spotify.com/v1/playlists/{spotify_id}/tracks?limit=100"
        while url:
            async with session.get(url, headers=headers) as r:
                data = await r.json()
                for item in data.get("items", []):
                    track = item.get("track")
                    if track and track.get("name"):
                        artist = track["artists"][0]["name"] if track.get("artists") else ""
                        song_queries.append(f"{track['name']} {artist}")
                url = data.get("next")  # วนดึงหน้าถัดไปจนกว่า playlist จะหมด

    return song_queries


# ==================== 1. คลาสปุ่มหน้าปัด (Dashboard) ====================
class MusicDashboard(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) 
        self.add_item(discord.ui.Button(label="เว็บของเรา", style=discord.ButtonStyle.link, url="https://www.khuiai.com/th/profile/Porkhyun"))

    @discord.ui.button(label="ฟังเพลงใหม่", style=discord.ButtonStyle.success, emoji="▶️", custom_id="play_btn")
    async def play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        help_text = """**🎵 คู่มือคำสั่งบอทเพลง Pork Hyun Radio:**
`!p` หรือ `!play <ชื่อเพลง / ลิงก์ YouTube / ลิงก์ Spotify>` : เรียกบอทเข้าห้องเสียงเพื่อเปิดเพลง
รองรับลิงก์ Spotify ทั้งเพลงเดี่ยว, อัลบั้ม และเพลย์ลิสต์ (ระบบจะแปลงเป็นค้นหาบน YouTube ให้อัตโนมัติ)
`!q` : เช็คคิวเพลง
`!skip` : สั่งข้ามเพลงที่กำลังเล่นอยู่ไปฟังเพลงถัดไป
`!pause` : สั่งหยุดเพลงชั่วคราว (พักเบรก)
`!resume` : สั่งให้เพลงที่หยุดไว้เล่นต่อจากเดิม
`!clear` หรือ `!c` : สั่งล้างคิวเพลงทั้งหมด
`!stop` : สั่งให้บอทหยุดเล่นเพลงและออกจากห้องเสียง"""
        await interaction.response.send_message(help_text, ephemeral=True)

    @discord.ui.button(label="Donate", style=discord.ButtonStyle.success, emoji="🪙", custom_id="donate_btn")
    async def donate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("💸 ขอบคุณที่สนับสนุนครับ! สามารถโดเนทได้ที่ห้อง <#1511062155640963072> เลยครับ!", ephemeral=True)


# ==================== 2. คลาสสถาปัตยกรรมระดับเทพ ====================
class OreoCloneBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        nodes = [wavelink.Node(uri="http://127.0.0.1:2333", password="youshallnotpass")]
        await wavelink.Pool.connect(client=self, nodes=nodes)
        self.add_view(MusicDashboard())

    async def on_ready(self):
        listen_status = discord.Activity(type=discord.ActivityType.listening, name="ฟังเพลง 24 ชม. | !play")
        await self.change_presence(status=discord.Status.online, activity=listen_status)
        print(f'✅ พระเจ้า {self.user} ประทับร่าง และพร้อมลุยแล้ว!')

bot = OreoCloneBot()

@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"🔥 เครื่องยนต์ Lavalink เชื่อมต่อสำเร็จ! พร้อมกระแทกหูฟัง!")


# ==================== 3. คลังคำสั่ง (Music Commands) ====================

@bot.command(name='dashboard', aliases=['setup'])
async def spawn_dashboard(ctx: commands.Context):
    embed = discord.Embed(
        description="```\nไม่มีเพลงที่กำลังเล่นอยู่ในขณะนี้\n```",
        color=0xffa500
    )
    embed.set_author(
        name="Prok Hyun Music Radio",
        icon_url="https://media2.giphy.com/media/v1.Y2lkPTc5MGI3NjExa2VyejJwZ2tmNGhzcTNoejN6ZjJzOG9pcHBxOTVzOTNwc2Fyb3k0MyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9cw/xKi7t0cYQwTLA69YYY/giphy.gif" 
    )
    embed.set_image(url="https://media4.giphy.com/media/v1.Y2lkPTc5MGI3NjExdDRwaDNra2l5ZXhwOXB3Mzlta3pyMTdkZzlwc2p1YjMxOHBjMnM4YSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/30MxopxLs9wbpzKIo6/giphy.gif") 
    view = MusicDashboard()
    await ctx.message.delete()
    msg = await ctx.send(embed=embed, view=view)
    bot.dashboard_msg = msg


@bot.command(name='play', aliases=['p'])
async def play(ctx: commands.Context, *, search: str):
    """🎵 สั่งเปิดเพลง (รองรับ Spotify (track/album/playlist), YouTube & ค้นหาชื่อเพลง)"""
    if not ctx.author.voice:
        return await ctx.send("❌ ไก่อ่อนเอ๊ย! นายต้องเข้าไปในห้องเสียงก่อนสิ!")

    vc: wavelink.Player = ctx.voice_client
    if not vc:
        vc = await ctx.author.voice.channel.connect(cls=wavelink.Player)

    try:
        # ---------------- ลิงก์ Spotify (track / album / playlist) ----------------
        if "spotify.com" in search:
            kind, spotify_id = parse_spotify_url(search)

            if not kind:
                return await ctx.send("❌ ลิงก์ Spotify นี้ไม่รองรับนะ (รองรับแค่ track / album / playlist)")

            if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
                return await ctx.send("❌ ยังไม่ได้ตั้งค่า Spotify API Key ในระบบ (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET)")

            loading_msg = await ctx.send(f"🔎 กำลังดึงข้อมูลจาก Spotify ({kind})...")

            async with aiohttp.ClientSession() as session:
                song_queries = await fetch_spotify_track_names(session, kind, spotify_id)

            if not song_queries:
                await loading_msg.delete()
                return await ctx.send("❌ ดึงข้อมูลจาก Spotify ไม่ได้ ลองเช็คลิงก์อีกครั้ง")

            added_count = 0
            first_track_title = None

            for query in song_queries:
                results = await wavelink.Playable.search(f"ytsearch:{query}")
                if not results:
                    continue  # หาเพลงนี้บน YouTube ไม่เจอ ข้ามไปเพลงถัดไป
                track = results[0]
                await vc.queue.put_wait(track)
                added_count += 1
                if first_track_title is None:
                    first_track_title = track.title

            await loading_msg.delete()

            if added_count == 0:
                return await ctx.send("❌ หาเพลงจาก Spotify บน YouTube ไม่เจอเลยสักเพลง")

            if kind == "track":
                await ctx.send(f"🎵 เพิ่มเข้าคิวจาก Spotify เรียบร้อย: **{first_track_title}**")
            else:
                await ctx.send(f"📋 แปลง Spotify {kind} เป็น YouTube สำเร็จ **{added_count}/{len(song_queries)}** เพลง เข้าคิวเรียบร้อย!")

        # ---------------- ไม่ใช่ Spotify ก็ค้นหา/เล่นตามปกติ (YouTube ลิงก์ หรือค้นชื่อเพลง) ----------------
        else:
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

        if not vc.playing:
            next_track = vc.queue.get()
            await vc.play(next_track)

    except Exception as e:
        await ctx.send(f"❌ ระบบขัดข้องระดับ Core: `{e}`")
        print(f"Error: {e}")


@bot.command(name='skip', aliases=['s'])
async def skip(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client
    if not vc or not vc.playing:
        return await ctx.send("❌ ไม่มีเพลงให้ข้ามเว้ย!")
    await vc.stop()
    msg = await ctx.send("⏭️ ข้ามเพลงให้แล้ว!")
    await msg.delete(delay=5)
   
@bot.command(name='pause')
async def pause(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client
    if vc and vc.playing:
        await vc.pause(True)
        msg = await ctx.send("⏸️ แช่แข็งเพลงชั่วคราว!")
        await msg.delete(delay=5)

@bot.command(name='resume', aliases=['r'])
async def resume(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client
    if vc and vc.paused:
        await vc.pause(False)
        msg = await ctx.send("▶️ ปลดล็อค! เล่นเพลงต่อแล้ว!")
        await msg.delete(delay=5)

@bot.command(name='queue', aliases=['q'])
async def show_queue(ctx: commands.Context):
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
    vc: wavelink.Player = ctx.voice_client
    if not vc:
        return await ctx.send("❌ บอทไม่ได้อยู่ในห้องเสียง!")
    if vc.queue.is_empty:
        msg = await ctx.send("📭 คิวโล่งอยู่แล้ว จะให้ล้างอะไรอีกล่ะ!")
        return await msg.delete(delay=5)
    vc.queue.clear()
    msg = await ctx.send("🧹 ล้างบางคิวเพลงที่รออยู่ทั้งหมดเรียบร้อยแล้ว!")
    await msg.delete(delay=5)

@bot.command(name='stop')
async def stop(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client
    if vc:
        vc.queue.clear() 
        await vc.disconnect() 
        if hasattr(bot, 'dashboard_msg') and bot.dashboard_msg:
            try:
                embed = bot.dashboard_msg.embeds[0]
                embed.description = "```\nไม่มีเพลงที่กำลังเล่นอยู่ในขณะนี้\n```"
                await bot.dashboard_msg.edit(embed=embed)
            except:
                pass
        msg = await ctx.send("🛑 ล้างบางคิวเพลง และเตะตัวเองกลับบ้านเรียบร้อย!")
        await msg.delete(delay=5)


# ==================== 4. ระบบจัดการเมื่อเพลงเริ่ม/จบ ====================

@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    if hasattr(bot, 'dashboard_msg') and bot.dashboard_msg:
        try:
            embed = bot.dashboard_msg.embeds[0]
            embed.description = f"```\n🎵 กำลังเล่น: {payload.track.title}\n```"
            await bot.dashboard_msg.edit(embed=embed)
        except:
            pass

@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    if not player:
        return

    if not player.queue.is_empty:
        next_track = await player.queue.get_wait()
        await player.play(next_track)
    else:
        if hasattr(bot, 'dashboard_msg') and bot.dashboard_msg:
            try:
                embed = bot.dashboard_msg.embeds[0]
                embed.description = "```\nไม่มีเพลงที่กำลังเล่นอยู่ในขณะนี้\n```"
                await bot.dashboard_msg.edit(embed=embed)
            except:
                pass
        try:
            msg = await player.channel.send("✅ คิวหมดแล้ว! บอทขอตัวกลับสวรรค์ก่อนนะ 💨")
            await msg.delete(delay=10)
        except:
            pass
        finally:
            await player.disconnect()


# ==================== 5. รันบอท ====================
bot.run(os.getenv("DISCORD_TOKEN"))
