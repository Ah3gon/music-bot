import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import asyncio
import random
import os
import asyncpg
import aiohttp
import re
import time
import base64
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
DATABASE_URL          = os.getenv("DATABASE_URL")
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
IDLE_TIMEOUT          = 300
EMPTY_CH_TIMEOUT      = 60
SPOTIFY_TRACK_LIMIT   = 50   # максимум треков из Spotify плейлиста/альбома

NODES = [
    {"uri": "http://lavalink.jirayu.net:13592", "password": "youshallnotpass"},
    {"uri": "http://n3.nexcloud.in:2026",       "password": "nexcloud"},
]

# ─────────────────────────────────────────────
#  Бот
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

idle_tasks: dict[int, asyncio.Task] = {}
db_pool: Optional[asyncpg.Pool] = None
# recording_sessions: guild_id -> {user_id, playlist_id, playlist_name}
recording_sessions: dict[int, dict] = {}
# Кэш Spotify токена
_spotify_token: Optional[str] = None
_spotify_token_expires: float = 0.0


# ─────────────────────────────────────────────
#  База данных
# ─────────────────────────────────────────────
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                name       TEXT   NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, name)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id          SERIAL  PRIMARY KEY,
                playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
                title       TEXT    NOT NULL,
                uri         TEXT    NOT NULL,
                duration    INTEGER NOT NULL,
                position    INTEGER NOT NULL
            )
        """)


async def db_create_playlist(user_id: int, name: str) -> Optional[int]:
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO playlists (user_id, name) VALUES ($1, $2) RETURNING id",
                user_id, name
            )
            return row["id"]
    except asyncpg.UniqueViolationError:
        return None


async def db_get_playlist(user_id: int, name: str) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM playlists WHERE user_id=$1 AND name=$2", user_id, name
        )
        return dict(row) if row else None


async def db_get_user_playlists(user_id: int) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id, p.name, COUNT(t.id) AS track_count "
            "FROM playlists p LEFT JOIN playlist_tracks t ON p.id=t.playlist_id "
            "WHERE p.user_id=$1 GROUP BY p.id ORDER BY p.created_at",
            user_id
        )
        return [dict(r) for r in rows]


async def db_delete_playlist(user_id: int, name: str) -> bool:
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM playlists WHERE user_id=$1 AND name=$2", user_id, name
        )
        return result != "DELETE 0"


async def db_add_track(playlist_id: int, title: str, uri: str, duration: int):
    async with db_pool.acquire() as conn:
        pos = await conn.fetchval(
            "SELECT COALESCE(MAX(position),0)+1 FROM playlist_tracks WHERE playlist_id=$1",
            playlist_id
        )
        await conn.execute(
            "INSERT INTO playlist_tracks (playlist_id,title,uri,duration,position) "
            "VALUES ($1,$2,$3,$4,$5)",
            playlist_id, title, uri, duration, pos
        )


async def db_get_tracks(playlist_id: int) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM playlist_tracks WHERE playlist_id=$1 ORDER BY position",
            playlist_id
        )
        return [dict(r) for r in rows]


async def db_remove_track(playlist_id: int, position: int) -> bool:
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id=$1 AND position=$2",
            playlist_id, position
        )
        return result != "DELETE 0"


# ─────────────────────────────────────────────
#  Spotify
# ─────────────────────────────────────────────
async def get_spotify_token() -> Optional[str]:
    global _spotify_token, _spotify_token_expires
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    if _spotify_token and time.time() < _spotify_token_expires:
        return _spotify_token
    creds = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "client_credentials"},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            _spotify_token = data["access_token"]
            _spotify_token_expires = time.time() + data["expires_in"] - 60
            return _spotify_token


def parse_spotify_url(url: str) -> Optional[tuple]:
    m = re.search(r'spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)', url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'spotify:(track|album|playlist):([A-Za-z0-9]+)', url)
    if m:
        return m.group(1), m.group(2)
    return None


async def fetch_spotify_tracks(url: str) -> Optional[list]:
    """Возвращает список {title, artist} или None."""
    parsed = parse_spotify_url(url)
    if not parsed:
        return None
    sp_type, sp_id = parsed
    token = await get_spotify_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    tracks = []
    async with aiohttp.ClientSession() as s:
        if sp_type == "track":
            async with s.get(
                f"https://api.spotify.com/v1/tracks/{sp_id}", headers=headers
            ) as r:
                if r.status != 200:
                    return None
                d = await r.json()
                tracks.append({"title": d["name"], "artist": d["artists"][0]["name"]})
        elif sp_type == "album":
            async with s.get(
                f"https://api.spotify.com/v1/albums/{sp_id}/tracks?limit=50",
                headers=headers,
            ) as r:
                if r.status != 200:
                    return None
                d = await r.json()
                for item in d["items"][:SPOTIFY_TRACK_LIMIT]:
                    tracks.append({"title": item["name"], "artist": item["artists"][0]["name"]})
        elif sp_type == "playlist":
            async with s.get(
                f"https://api.spotify.com/v1/playlists/{sp_id}/tracks?limit=50",
                headers=headers,
            ) as r:
                if r.status != 200:
                    return None
                d = await r.json()
                for item in d["items"][:SPOTIFY_TRACK_LIMIT]:
                    t = item.get("track")
                    if t:
                        tracks.append({"title": t["name"], "artist": t["artists"][0]["name"]})
    return tracks or None


# ─────────────────────────────────────────────
#  Утилиты
# ─────────────────────────────────────────────
def format_duration(ms: int) -> str:
    seconds = ms // 1000
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


async def start_idle_timer(guild: discord.Guild, channel: discord.TextChannel):
    if guild.id in idle_tasks:
        idle_tasks[guild.id].cancel()

    async def _timer():
        await asyncio.sleep(IDLE_TIMEOUT)
        player: wavelink.Player = guild.voice_client
        if player and not player.playing and not player.paused:
            await player.disconnect()
            idle_tasks.pop(guild.id, None)
            await channel.send(f"💤 Вышел — {IDLE_TIMEOUT // 60} мин тишины.")

    idle_tasks[guild.id] = asyncio.create_task(_timer())


def cancel_idle_timer(guild_id: int):
    if guild_id in idle_tasks:
        idle_tasks[guild_id].cancel()
        del idle_tasks[guild_id]


# ─────────────────────────────────────────────
#  Модальные окна
# ─────────────────────────────────────────────
class QueueModal(discord.ui.Modal, title="Очередь треков"):
    count = discord.ui.TextInput(
        label="Сколько треков показать?",
        placeholder="Оставь пустым чтобы показать все",
        required=False,
        max_length=3,
    )

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        player: wavelink.Player = self.guild.voice_client
        if not player or (not player.current and player.queue.is_empty):
            await interaction.response.send_message("📭 Очередь пуста.", ephemeral=True)
            return
        try:
            limit = int(self.count.value) if self.count.value.strip() else None
        except ValueError:
            await interaction.response.send_message("❗ Введи число.", ephemeral=True)
            return
        lines = []
        if player.current:
            t = player.current
            link = f" — [открыть]({t.uri})" if t.uri else ""
            lines.append(f"🎵 **Сейчас:** {t.title} `[{format_duration(t.length)}]`{link}\n")
        if not player.queue.is_empty:
            queue_list = list(player.queue)
            total = len(queue_list)
            shown = queue_list[:limit] if limit else queue_list[:20]
            lines.append("**В очереди:**")
            for i, t in enumerate(shown, 1):
                lines.append(f"`{i}.` 🎵 {t.title} `[{format_duration(t.length)}]`")
            if limit and total > limit:
                lines.append(f"_...и ещё {total - limit} треков_")
            elif not limit and total > 20:
                lines.append(f"_...и ещё {total - 20} треков_")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class AddToPlaylistModal(discord.ui.Modal, title="Добавить в плейлист"):
    playlist_name = discord.ui.TextInput(
        label="Название плейлиста",
        placeholder="Введи название плейлиста",
        required=True,
        max_length=50,
    )

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.current:
            await interaction.response.send_message("❗ Ничего не играет.", ephemeral=True)
            return
        name = self.playlist_name.value.strip()
        playlist = await db_get_playlist(interaction.user.id, name)
        if not playlist:
            await interaction.response.send_message(
                f"❗ Плейлист **{name}** не найден. Создай через `/playlist create {name}`",
                ephemeral=True,
            )
            return
        track = player.current
        await db_add_track(playlist["id"], track.title, track.uri, track.length)
        await interaction.response.send_message(
            f"✅ **{track.title}** добавлен в плейлист **{name}**!", ephemeral=True
        )


# ─────────────────────────────────────────────
#  Кнопки управления под "Сейчас играет"
# ─────────────────────────────────────────────
class PlayerControls(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild

    @property
    def player(self) -> Optional[wavelink.Player]:
        return self.guild.voice_client

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p and p.playing and not p.paused:
            await p.pause(True)
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
        elif p and p.paused:
            await p.pause(False)
            button.emoji = "⏸"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p and (p.playing or p.paused):
            await p.skip(force=True)
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        modes = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
        labels = {
            wavelink.QueueMode.normal:   "Повтор выкл ➡️",
            wavelink.QueueMode.loop:     "Повтор трека 🔂",
            wavelink.QueueMode.loop_all: "Повтор очереди 🔁",
        }
        current = p.queue.mode
        next_mode = modes[(modes.index(current) + 1) % 3]
        p.queue.mode = next_mode
        await interaction.response.send_message(labels[next_mode], ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p and len(p.queue) > 1:
            p.queue.shuffle()
            await interaction.response.send_message("🔀 Очередь перемешана.", ephemeral=True)
        else:
            await interaction.response.send_message("❗ Нечего перемешивать.", ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QueueModal(self.guild))

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.secondary, row=1)
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddToPlaylistModal(self.guild))

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p:
            p.queue.clear()
            cancel_idle_timer(self.guild.id)
            await p.stop()
            await p.disconnect()
        await interaction.response.send_message("⏹ Остановлено.", ephemeral=True)


# ─────────────────────────────────────────────
#  View: выбор трека из результатов поиска
# ─────────────────────────────────────────────
class TrackSelectView(discord.ui.View):
    def __init__(self, tracks: list, guild: discord.Guild,
                 voice_channel: discord.VoiceChannel,
                 text_channel: discord.TextChannel,
                 search_msg: discord.Message):
        super().__init__(timeout=60)
        self.tracks = tracks
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.search_msg = search_msg
        for i in range(len(tracks)):
            btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary)
            btn.callback = self._make_cb(i)
            self.add_item(btn)
        cancel = discord.ui.Button(label="✖ Отмена", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _make_cb(self, index: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            track = self.tracks[index]
            player: wavelink.Player = self.guild.voice_client
            if player is None:
                player = await self.voice_channel.connect(cls=wavelink.Player)
            elif player.channel != self.voice_channel:
                await player.move_to(self.voice_channel)
            player.autoplay = wavelink.AutoPlayMode.disabled
            player._text_channel_id = self.text_channel.id
            if not player.playing:
                await self.search_msg.delete()
                await player.play(track)
            else:
                await player.queue.put_wait(track)
                await self.search_msg.edit(
                    content=f"➕ **Добавлено:** {track.title} `[{format_duration(track.length)}]`",
                    view=None,
                )
            cancel_idle_timer(self.guild.id)
            self.stop()
        return callback

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.search_msg.edit(content="❌ Отменено.", view=None)
        self.stop()


# ─────────────────────────────────────────────
#  События Wavelink
# ─────────────────────────────────────────────
@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player = payload.player
    track = payload.track
    guild = player.guild

    # Запись в плейлист
    if guild.id in recording_sessions:
        session = recording_sessions[guild.id]
        await db_add_track(session["playlist_id"], track.title, track.uri, track.length)

    channel_id = getattr(player, "_text_channel_id", None)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not channel:
        return

    msg_id = getattr(player, "_now_playing_msg_id", None)
    if msg_id:
        try:
            old = await channel.fetch_message(msg_id)
            await old.delete()
        except Exception:
            pass

    loop_labels = {
        wavelink.QueueMode.normal:   "выкл ➡️",
        wavelink.QueueMode.loop:     "трек 🔂",
        wavelink.QueueMode.loop_all: "очередь 🔁",
    }
    recording_indicator = ""
    if guild.id in recording_sessions:
        recording_indicator = f" | 🔴 Запись: **{recording_sessions[guild.id]['playlist_name']}**"

    link = f" — [открыть]({track.uri})" if track.uri else ""
    text = (
        f"🎵 **Сейчас играет:** {track.title} "
        f"`[{format_duration(track.length)}]`{link}\n"
        f"Повтор: **{loop_labels[player.queue.mode]}**{recording_indicator}"
    )
    msg = await channel.send(text, view=PlayerControls(guild))
    player._now_playing_msg_id = msg.id


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    guild = player.guild
    channel_id = getattr(player, "_text_channel_id", None)
    channel = guild.get_channel(channel_id) if channel_id else None

    if player.queue.mode == wavelink.QueueMode.loop:
        await player.play(payload.track)
        return
    if player.queue.mode == wavelink.QueueMode.loop_all:
        await player.queue.put_wait(payload.track)
    if not player.queue.is_empty:
        await player.play(player.queue.get())
    elif channel:
        await start_idle_timer(guild, channel)


@bot.event
async def on_wavelink_inactive_player(player: wavelink.Player):
    cancel_idle_timer(player.guild.id)
    await player.disconnect()


# ─────────────────────────────────────────────
#  Slash-команды: воспроизведение
# ─────────────────────────────────────────────
PLATFORM_CHOICES = [
    app_commands.Choice(name="YouTube",    value="yt"),
    app_commands.Choice(name="SoundCloud", value="sc"),
]


@tree.command(name="play", description="Поиск трека или добавление плейлиста")
@app_commands.describe(query="Название трека, ссылка на видео или плейлист", platform="Платформа поиска")
@app_commands.choices(platform=PLATFORM_CHOICES)
async def play_cmd(interaction: discord.Interaction, query: str,
                   platform: app_commands.Choice[str] = None):
    await interaction.response.defer()
    if not interaction.user.voice:
        await interaction.followup.send("❗ Зайди в голосовой канал сначала.")
        return

    msg = await interaction.followup.send(f"🔍 Ищу **{query}**...", wait=True)

    source = wavelink.TrackSource.SoundCloud if (platform and platform.value == "sc") \
        else wavelink.TrackSource.YouTube
    results = await wavelink.Playable.search(query, source=source)

    if not results:
        await msg.edit(content="😕 Ничего не найдено.")
        return

    player: wavelink.Player = interaction.guild.voice_client
    if player is None:
        player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
    elif player.channel != interaction.user.voice.channel:
        await player.move_to(interaction.user.voice.channel)
    player.autoplay = wavelink.AutoPlayMode.disabled
    player._text_channel_id = interaction.channel.id
    cancel_idle_timer(interaction.guild_id)

    # Плейлист
    if isinstance(results, wavelink.Playlist):
        for track in results.tracks:
            await player.queue.put_wait(track)
        if not player.playing:
            await msg.delete()
            await player.play(player.queue.get())
        else:
            await msg.edit(
                content=f"📋 **Плейлист добавлен:** {results.name} — `{len(results.tracks)} треков`",
                view=None,
            )
        return

    # Одиночная ссылка
    if query.startswith("http") and len(results) == 1:
        track = results[0]
        await player.queue.put_wait(track)
        if not player.playing:
            await msg.delete()
            await player.play(player.queue.get())
        else:
            await msg.edit(
                content=f"➕ **Добавлено:** {track.title} `[{format_duration(track.length)}]`",
                view=None,
            )
        return

    # Обычный поиск
    tracks = results[:5]
    lines = ["**Результаты поиска:**\n"]
    for i, t in enumerate(tracks, 1):
        lines.append(f"`{i}.` {t.title} `[{format_duration(t.length)}]`")
    lines.append("\nВыбери трек кнопкой:")
    view = TrackSelectView(tracks, interaction.guild,
                           interaction.user.voice.channel, interaction.channel, msg)
    await msg.edit(content="\n".join(lines), view=view)


@tree.command(name="spotify", description="Добавить трек/альбом/плейлист из Spotify")
@app_commands.describe(url="Ссылка на Spotify")
async def spotify_cmd(interaction: discord.Interaction, url: str):
    await interaction.response.defer()
    if not interaction.user.voice:
        await interaction.followup.send("❗ Зайди в голосовой канал сначала.")
        return
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        await interaction.followup.send(
            "❗ Spotify не настроен. Добавь `SPOTIFY_CLIENT_ID` и `SPOTIFY_CLIENT_SECRET` в Variables на Railway."
        )
        return

    msg = await interaction.followup.send("🎵 Получаю треки из Spotify...", wait=True)
    spotify_tracks = await fetch_spotify_tracks(url)
    if not spotify_tracks:
        await msg.edit(content="❗ Не удалось получить треки. Проверь ссылку.")
        return

    player: wavelink.Player = interaction.guild.voice_client
    if player is None:
        player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
    elif player.channel != interaction.user.voice.channel:
        await player.move_to(interaction.user.voice.channel)
    player.autoplay = wavelink.AutoPlayMode.disabled
    player._text_channel_id = interaction.channel.id
    cancel_idle_timer(interaction.guild_id)

    await msg.edit(content=f"🔍 Ищу {len(spotify_tracks)} треков на YouTube...")
    added = 0
    for sp in spotify_tracks:
        results = await wavelink.Playable.search(
            f"{sp['artist']} - {sp['title']}", source=wavelink.TrackSource.YouTube
        )
        if results:
            track = results[0] if isinstance(results, list) else results.tracks[0]
            await player.queue.put_wait(track)
            added += 1

    if added == 0:
        await msg.edit(content="😕 Не удалось найти треки на YouTube.")
        return
    if not player.playing:
        await msg.delete()
        await player.play(player.queue.get())
    else:
        await msg.edit(content=f"📋 Добавлено из Spotify: `{added} треков`")


@tree.command(name="savequeue", description="Сохранить текущую очередь как плейлист")
@app_commands.describe(name="Название нового плейлиста")
async def savequeue_cmd(interaction: discord.Interaction, name: str):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or (not player.current and player.queue.is_empty):
        await interaction.response.send_message("❗ Очередь пуста.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    playlist_id = await db_create_playlist(interaction.user.id, name.strip())
    if playlist_id is None:
        await interaction.followup.send(f"❗ Плейлист **{name}** уже существует.")
        return
    tracks = []
    if player.current:
        tracks.append(player.current)
    tracks.extend(list(player.queue))
    for t in tracks:
        await db_add_track(playlist_id, t.title, t.uri, t.length)
    await interaction.followup.send(
        f"✅ Очередь сохранена как плейлист **{name}** — `{len(tracks)} треков`!"
    )


# ─────────────────────────────────────────────
#  Slash-команды: управление плеером
# ─────────────────────────────────────────────
@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and (player.playing or player.paused):
        await player.skip(force=True)
        await interaction.response.defer()
    else:
        await interaction.response.send_message("❗ Ничего не играет.")


@tree.command(name="skipto", description="Перейти к треку по номеру в очереди")
@app_commands.describe(position="Номер трека")
async def skipto_cmd(interaction: discord.Interaction, position: int):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(f"❗ Укажи номер от 1 до {len(q)}.")
        return
    for _ in range(position - 1):
        q.get()
    await player.skip(force=True)
    await interaction.response.send_message(f"⏩ Перехожу к треку #{position}.")


@tree.command(name="pause", description="Пауза / продолжить")
async def pause_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and player.playing and not player.paused:
        await player.pause(True)
        await interaction.response.send_message("⏸ Пауза.")
    elif player and player.paused:
        await player.pause(False)
        await interaction.response.send_message("▶️ Продолжаю.")
    else:
        await interaction.response.send_message("❗ Ничего не играет.")


@tree.command(name="stop", description="Остановить и очистить очередь")
async def stop_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        player.queue.clear()
        cancel_idle_timer(interaction.guild_id)
        await player.stop()
        await player.disconnect()
        await interaction.response.send_message("⏹ Остановлено.")
    else:
        await interaction.response.send_message("❗ Бот не в канале.")


@tree.command(name="volume", description="Громкость от 0 до 100")
@app_commands.describe(level="Уровень громкости (0–100)")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        await interaction.response.send_message("❗ Укажи число от 0 до 100.")
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    await player.set_volume(level)
    await interaction.response.send_message(f"🔊 Громкость: **{level}%**")


@tree.command(name="loop", description="Переключить режим повтора")
async def loop_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    modes = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
    labels = {
        wavelink.QueueMode.normal:   "выключен ➡️",
        wavelink.QueueMode.loop:     "повтор трека 🔂",
        wavelink.QueueMode.loop_all: "повтор очереди 🔁",
    }
    current = player.queue.mode
    next_mode = modes[(modes.index(current) + 1) % 3]
    player.queue.mode = next_mode
    await interaction.response.send_message(f"Повтор: **{labels[next_mode]}**")


@tree.command(name="shuffle", description="Перемешать очередь")
async def shuffle_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and len(player.queue) > 1:
        player.queue.shuffle()
        await interaction.response.send_message("🔀 Очередь перемешана.")
    else:
        await interaction.response.send_message("❗ Нечего перемешивать.")


@tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер трека в очереди")
async def remove_cmd(interaction: discord.Interaction, position: int):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(f"❗ Укажи номер от 1 до {len(q)}.")
        return
    track = q[position - 1]
    del q[position - 1]
    await interaction.response.send_message(f"🗑 Удалено: **{track.title}**")


@tree.command(name="queue", description="Показать очередь")
async def queue_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or (not player.current and player.queue.is_empty):
        await interaction.response.send_message("📭 Очередь пуста.")
        return
    lines = []
    if player.current:
        t = player.current
        link = f" — [открыть]({t.uri})" if t.uri else ""
        lines.append(f"🎵 **Сейчас:** {t.title} `[{format_duration(t.length)}]`{link}\n")
    if not player.queue.is_empty:
        lines.append("**В очереди:**")
        for i, t in enumerate(list(player.queue)[:10], 1):
            lines.append(f"`{i}.` 🎵 {t.title} `[{format_duration(t.length)}]`")
        if len(player.queue) > 10:
            lines.append(f"_...и ещё {len(player.queue) - 10} треков_")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="nowplaying", description="Что сейчас играет")
async def np_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message("📭 Ничего не играет.")
        return
    t = player.current
    link = f" — [открыть]({t.uri})" if t.uri else ""
    await interaction.response.send_message(
        f"🎵 **Сейчас играет:** {t.title} `[{format_duration(t.length)}]`{link}"
    )


# ─────────────────────────────────────────────
#  Slash-команды: плейлисты
# ─────────────────────────────────────────────
playlist_group = app_commands.Group(name="playlist", description="Управление личными плейлистами")


@playlist_group.command(name="create", description="Создать новый плейлист")
@app_commands.describe(name="Название плейлиста")
async def pl_create(interaction: discord.Interaction, name: str):
    playlist_id = await db_create_playlist(interaction.user.id, name.strip())
    if playlist_id is None:
        await interaction.response.send_message(f"❗ Плейлист **{name}** уже существует.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Плейлист **{name}** создан!", ephemeral=True)


@playlist_group.command(name="list", description="Показать твои плейлисты")
async def pl_list(interaction: discord.Interaction):
    playlists = await db_get_user_playlists(interaction.user.id)
    if not playlists:
        await interaction.response.send_message(
            "📭 У тебя нет плейлистов. Создай через `/playlist create`", ephemeral=True
        )
        return
    lines = ["**Твои плейлисты:**\n"]
    for i, p in enumerate(playlists, 1):
        rec = recording_sessions.get(interaction.guild_id, {})
        indicator = "🔴 " if rec.get("playlist_id") == p["id"] else ""
        lines.append(f"`{i}.` {indicator}**{p['name']}** — {p['track_count']} треков")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@playlist_group.command(name="play", description="Воспроизвести плейлист")
@app_commands.describe(name="Название плейлиста")
async def pl_play(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    if not interaction.user.voice:
        await interaction.followup.send("❗ Зайди в голосовой канал сначала.")
        return
    playlist = await db_get_playlist(interaction.user.id, name.strip())
    if not playlist:
        await interaction.followup.send(f"❗ Плейлист **{name}** не найден.")
        return
    tracks = await db_get_tracks(playlist["id"])
    if not tracks:
        await interaction.followup.send(f"❗ Плейлист **{name}** пуст.")
        return

    player: wavelink.Player = interaction.guild.voice_client
    if player is None:
        player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
    elif player.channel != interaction.user.voice.channel:
        await player.move_to(interaction.user.voice.channel)
    player.autoplay = wavelink.AutoPlayMode.disabled
    player._text_channel_id = interaction.channel.id
    cancel_idle_timer(interaction.guild_id)

    msg = await interaction.followup.send(f"⏳ Загружаю плейлист **{name}**...", wait=True)
    added = 0
    for t in tracks:
        results = await wavelink.Playable.search(t["uri"])
        if results:
            track = results[0] if isinstance(results, list) else results.tracks[0]
            await player.queue.put_wait(track)
            added += 1

    if added == 0:
        await msg.edit(content=f"😕 Не удалось загрузить треки из **{name}**.")
        return
    if not player.playing:
        await msg.delete()
        await player.play(player.queue.get())
    else:
        await msg.edit(content=f"📋 Плейлист **{name}** добавлен в очередь — `{added} треков`")


@playlist_group.command(name="delete", description="Удалить плейлист")
@app_commands.describe(name="Название плейлиста")
async def pl_delete(interaction: discord.Interaction, name: str):
    # Останавливаем запись если нужно
    rec = recording_sessions.get(interaction.guild_id, {})
    if rec.get("playlist_name") == name.strip():
        recording_sessions.pop(interaction.guild_id, None)
    deleted = await db_delete_playlist(interaction.user.id, name.strip())
    if not deleted:
        await interaction.response.send_message(f"❗ Плейлист **{name}** не найден.", ephemeral=True)
        return
    await interaction.response.send_message(f"🗑 Плейлист **{name}** удалён.", ephemeral=True)


@playlist_group.command(name="record", description="Начать/остановить запись треков в плейлист")
@app_commands.describe(name="Название плейлиста (не нужно при остановке)")
async def pl_record(interaction: discord.Interaction, name: Optional[str] = None):
    guild_id = interaction.guild_id
    if guild_id in recording_sessions:
        session = recording_sessions.pop(guild_id)
        await interaction.response.send_message(
            f"⏹ Запись в плейлист **{session['playlist_name']}** остановлена.", ephemeral=True
        )
        return
    if not name:
        await interaction.response.send_message(
            "❗ Укажи название плейлиста для начала записи.", ephemeral=True
        )
        return
    playlist = await db_get_playlist(interaction.user.id, name.strip())
    if not playlist:
        await interaction.response.send_message(
            f"❗ Плейлист **{name}** не найден. Создай через `/playlist create {name}`",
            ephemeral=True,
        )
        return
    recording_sessions[guild_id] = {
        "user_id": interaction.user.id,
        "playlist_id": playlist["id"],
        "playlist_name": name.strip(),
    }
    await interaction.response.send_message(
        f"🔴 Запись в **{name}** начата — все треки которые будут играть добавятся автоматически.\n"
        f"Чтобы остановить — `/playlist record` без аргументов.",
        ephemeral=True,
    )


@playlist_group.command(name="addtrack", description="Добавить текущий трек в плейлист")
@app_commands.describe(name="Название плейлиста")
async def pl_addtrack(interaction: discord.Interaction, name: str):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message("❗ Ничего не играет.", ephemeral=True)
        return
    playlist = await db_get_playlist(interaction.user.id, name.strip())
    if not playlist:
        await interaction.response.send_message(f"❗ Плейлист **{name}** не найден.", ephemeral=True)
        return
    track = player.current
    await db_add_track(playlist["id"], track.title, track.uri, track.length)
    await interaction.response.send_message(
        f"✅ **{track.title}** добавлен в **{name}**!", ephemeral=True
    )


@playlist_group.command(name="tracks", description="Показать треки плейлиста")
@app_commands.describe(name="Название плейлиста")
async def pl_tracks(interaction: discord.Interaction, name: str):
    playlist = await db_get_playlist(interaction.user.id, name.strip())
    if not playlist:
        await interaction.response.send_message(f"❗ Плейлист **{name}** не найден.", ephemeral=True)
        return
    tracks = await db_get_tracks(playlist["id"])
    if not tracks:
        await interaction.response.send_message(f"📭 Плейлист **{name}** пуст.", ephemeral=True)
        return
    lines = [f"**Плейлист: {name}** — {len(tracks)} треков\n"]
    for t in tracks[:20]:
        lines.append(f"`{t['position']}.` {t['title']} `[{format_duration(t['duration'])}]`")
    if len(tracks) > 20:
        lines.append(f"_...и ещё {len(tracks) - 20} треков_")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


tree.add_command(playlist_group)


@tree.command(name="help", description="Список всех команд бота")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎵 Music Bot — Команды",
        color=discord.Color.blurple()
    )

    embed.add_field(name="▶️ Воспроизведение", value=
        "`/play <запрос>` — поиск на YouTube или SoundCloud\n"
        "`/spotify <ссылка>` — добавить трек/альбом/плейлист из Spotify\n"
        "`/skip` — пропустить трек\n"
        "`/skipto <номер>` — перейти к треку в очереди\n"
        "`/pause` — пауза / продолжить\n"
        "`/stop` — остановить и выйти из канала",
        inline=False
    )

    embed.add_field(name="🎛️ Настройки", value=
        "`/volume <0-100>` — громкость\n"
        "`/loop` — режим повтора (выкл / трек / очередь)\n"
        "`/shuffle` — перемешать очередь",
        inline=False
    )

    embed.add_field(name="📋 Очередь", value=
        "`/queue` — показать очередь\n"
        "`/nowplaying` — текущий трек\n"
        "`/remove <номер>` — убрать трек из очереди\n"
        "`/savequeue <название>` — сохранить очередь как плейлист",
        inline=False
    )

    embed.add_field(name="💾 Плейлисты", value=
        "`/playlist create <название>` — создать плейлист\n"
        "`/playlist list` — твои плейлисты\n"
        "`/playlist play <название>` — воспроизвести плейлист\n"
        "`/playlist tracks <название>` — треки в плейлисте\n"
        "`/playlist addtrack <название>` — добавить текущий трек\n"
        "`/playlist record <название>` — начать/остановить запись\n"
        "`/playlist delete <название>` — удалить плейлист",
        inline=False
    )

    embed.add_field(name="🎮 Кнопки на панели", value=
        "⏸ пауза/продолжить  ⏭ пропустить  🔁 повтор\n"
        "🔀 перемешать  📋 очередь  💾 в плейлист  ⏹ стоп",
        inline=False
    )

    embed.set_footer(text="Подсказка: используй /play с выбором платформы для SoundCloud!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  Автовыход из пустого канала
# ─────────────────────────────────────────────
@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
    if member.bot:
        return
    player: wavelink.Player = member.guild.voice_client
    if not player:
        return
    if after.channel and after.channel == player.channel:
        cancel_idle_timer(member.guild.id)
        return
    non_bots = [m for m in player.channel.members if not m.bot]
    if len(non_bots) == 0:
        await asyncio.sleep(EMPTY_CH_TIMEOUT)
        if player.is_connected():
            non_bots = [m for m in player.channel.members if not m.bot]
            if len(non_bots) == 0:
                channel_id = getattr(player, "_text_channel_id", None)
                cancel_idle_timer(member.guild.id)
                await player.disconnect()
                if channel_id:
                    channel = member.guild.get_channel(channel_id)
                    if channel:
                        await channel.send("👋 Все ушли — выхожу из канала.")


# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Бот запущен как {bot.user}")
    if DATABASE_URL:
        await init_db()
        print("✅ База данных подключена")
    else:
        print("⚠️ DATABASE_URL не задан — плейлисты недоступны")
    nodes = [wavelink.Node(**n) for n in NODES]
    await wavelink.Pool.connect(nodes=nodes, client=bot)
    await tree.sync()
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="/play"
    ))


bot.run(DISCORD_TOKEN)
