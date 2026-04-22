import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import asyncio
import os
import asyncpg
import aiohttp
import re
import time
import base64
import logging
import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Логирование
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("surge")

# ─────────────────────────────────────────────
#  Опциональный импорт Яндекс.Музыки
# ─────────────────────────────────────────────
try:
    from yandex_music import ClientAsync as YMClient
    YANDEX_MUSIC_AVAILABLE = True
except ImportError:
    YANDEX_MUSIC_AVAILABLE = False
    YMClient = None
    log.warning("yandex-music не установлен — Я.М ссылки обрабатываться не будут")

# ─────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
DATABASE_URL          = os.getenv("DATABASE_URL")
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
GENIUS_TOKEN          = os.getenv("GENIUS_TOKEN", "")
IDLE_TIMEOUT          = 300
EMPTY_CH_TIMEOUT      = 60
SPOTIFY_TRACK_LIMIT   = 200
PLAYLIST_TRACK_LIMIT  = 200
BOT_NAME              = "Surge"
HISTORY_LIMIT         = 20
QUEUE_DEFAULT_SHOW    = 15
PLAYLIST_TRACKS_SHOW  = 20
LYRICS_MAX_CHARS      = 3900
PLAYLIST_NAME_MAX     = 50
SEEK_MAX_SECONDS      = 3600
TRACKS_PER_PAGE       = 15

EFFECTS = {
    "bassboost": "басс-буст 🔈",
    "nightcore": "nightcore 🌙",
    "vaporwave": "vaporwave 🌊",
    "slowmo":    "замедление 🐢",
    "8d":        "8D аудио 🎧",
    "off":       "выкл ➡️",
}

NODES = [
    {"uri": "http://lavalink.jirayu.net:13592", "password": "youshallnotpass"},
    {"uri": "http://n3.nexcloud.in:2026",       "password": "nexcloud"},
]

MAX_INT32 = 2147483647

# ─────────────────────────────────────────────
#  Бот
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

idle_tasks: dict[int, asyncio.Task] = {}
empty_channel_tasks: dict[int, asyncio.Task] = {}
db_pool: Optional[asyncpg.Pool] = None
track_history: dict[int, list] = {}
current_effect: dict[int, str] = {}
player_state: dict[int, dict] = {}
user_track_counts: dict[int, dict[int, int]] = {}
active_votes: dict[int, "VoteSkipView"] = {}
# Метки треков для fair_queue: {guild_id: {track.identifier: user_id}}
track_user_map: dict[int, dict[str, int]] = {}
_spotify_token: Optional[str] = None
_spotify_token_expires: float = 0.0
_ready_once = False


# ─────────────────────────────────────────────
#  Хелперы состояния плеера
# ─────────────────────────────────────────────
def get_player_state(guild_id: int) -> dict:
    return player_state.setdefault(guild_id, {})


def clear_player_state(guild_id: int):
    player_state.pop(guild_id, None)


# ─────────────────────────────────────────────
#  База данных
# ─────────────────────────────────────────────
async def init_db():
    global db_pool
    if db_pool is not None:
        return
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS birthdays (
                user_id      BIGINT PRIMARY KEY,
                birth_day    INTEGER NOT NULL,
                birth_month  INTEGER NOT NULL,
                birthday_song TEXT DEFAULT 'Happy Birthday instrumental'
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                guild_id      BIGINT PRIMARY KEY,
                tracks_played INTEGER DEFAULT 0,
                total_ms      BIGINT  DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id           BIGINT PRIMARY KEY,
                dj_role_id         BIGINT  DEFAULT NULL,
                vote_skip_enabled  BOOLEAN DEFAULT FALSE,
                vote_skip_percent  INTEGER DEFAULT 50,
                track_limit        INTEGER DEFAULT 0,
                fair_queue         BOOLEAN DEFAULT FALSE
            )
        """)
        # Миграции (безопасно добавляют колонки если их не было)
        for migration in [
            "ALTER TABLE birthdays ADD COLUMN IF NOT EXISTS "
            "birthday_song TEXT DEFAULT 'Happy Birthday instrumental'",
            "ALTER TABLE server_settings ADD COLUMN IF NOT EXISTS "
            "fair_queue BOOLEAN DEFAULT FALSE",
        ]:
            try:
                await conn.execute(migration)
            except Exception as e:
                log.debug("Migration skipped: %s", e)


async def db_get_settings(guild_id: int) -> dict:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM server_settings WHERE guild_id=$1", guild_id
        )
        if row:
            return dict(row)
        return {
            "guild_id": guild_id,
            "dj_role_id": None,
            "vote_skip_enabled": False,
            "vote_skip_percent": 50,
            "track_limit": 0,
            "fair_queue": False,
        }


async def db_save_settings(guild_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kwargs))
    values = list(kwargs.values())
    async with db_pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO server_settings (guild_id, {', '.join(kwargs.keys())}) "
            f"VALUES ($1, {', '.join(f'${i+2}' for i in range(len(kwargs)))}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {fields}",
            guild_id, *values
        )


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
    safe_duration = max(0, min(duration, MAX_INT32))
    async with db_pool.acquire() as conn:
        pos = await conn.fetchval(
            "SELECT COALESCE(MAX(position),0)+1 FROM playlist_tracks WHERE playlist_id=$1",
            playlist_id
        )
        await conn.execute(
            "INSERT INTO playlist_tracks (playlist_id,title,uri,duration,position) "
            "VALUES ($1,$2,$3,$4,$5)",
            playlist_id, title, uri, safe_duration, pos
        )


async def db_get_tracks(playlist_id: int) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM playlist_tracks WHERE playlist_id=$1 ORDER BY position",
            playlist_id
        )
        return [dict(r) for r in rows]


async def db_set_birthday(user_id: int, day: int, month: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO birthdays (user_id, birth_day, birth_month) VALUES ($1,$2,$3) "
            "ON CONFLICT (user_id) DO UPDATE SET birth_day=$2, birth_month=$3",
            user_id, day, month
        )


async def db_get_birthday(user_id: int) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM birthdays WHERE user_id=$1", user_id)
        return dict(row) if row else None


async def db_increment_stats(guild_id: int, duration_ms: int):
    safe_duration = max(0, min(duration_ms, MAX_INT32))
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO stats (guild_id, tracks_played, total_ms) VALUES ($1, 1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET "
            "tracks_played = stats.tracks_played + 1, "
            "total_ms = stats.total_ms + $2",
            guild_id, safe_duration
        )


async def db_get_stats(guild_id: int) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM stats WHERE guild_id=$1", guild_id)
        return dict(row) if row else None


# ─────────────────────────────────────────────
#  Проверка прав DJ
# ─────────────────────────────────────────────
async def is_dj(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if not db_pool:
        return True
    settings = await db_get_settings(member.guild.id)
    dj_role_id = settings.get("dj_role_id")
    if not dj_role_id:
        return True
    return any(r.id == dj_role_id for r in member.roles)


async def check_dj(interaction: discord.Interaction) -> bool:
    if not await is_dj(interaction.user):
        if interaction.response.is_done():
            await interaction.followup.send("❗ Для этого нужна роль **DJ**.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❗ Для этого нужна роль **DJ**.", ephemeral=True
            )
        return False
    return True


# ─────────────────────────────────────────────
#  Лимит треков
# ─────────────────────────────────────────────
def increment_user_track_count(guild_id: int, user_id: int, count: int = 1):
    if guild_id not in user_track_counts:
        user_track_counts[guild_id] = {}
    user_track_counts[guild_id][user_id] = \
        user_track_counts[guild_id].get(user_id, 0) + count


def get_user_track_count(guild_id: int, user_id: int) -> int:
    return user_track_counts.get(guild_id, {}).get(user_id, 0)


def reset_track_counts(guild_id: int):
    user_track_counts.pop(guild_id, None)


async def check_track_limit(interaction: discord.Interaction, count: int = 1) -> bool:
    if not db_pool:
        return True
    if await is_dj(interaction.user):
        return True
    settings = await db_get_settings(interaction.guild_id)
    limit = settings.get("track_limit", 0)
    if limit <= 0:
        return True
    current = get_user_track_count(interaction.guild_id, interaction.user.id)
    if current + count > limit:
        msg = (
            f"❗ Ты достиг лимита треков (**{limit}** за сессию). "
            f"Дождись конца очереди или попроси DJ добавить больше."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False
    return True


# ─────────────────────────────────────────────
#  Fair queue: метки треков
# ─────────────────────────────────────────────
def tag_track(guild_id: int, track, user_id: int):
    gmap = track_user_map.setdefault(guild_id, {})
    gmap[track.identifier] = user_id


def get_track_owner(guild_id: int, track) -> Optional[int]:
    return track_user_map.get(guild_id, {}).get(track.identifier)


def cleanup_track_map(guild_id: int, keep_tracks: list):
    gmap = track_user_map.get(guild_id)
    if not gmap:
        return
    keep_ids = {t.identifier for t in keep_tracks}
    for tid in list(gmap.keys()):
        if tid not in keep_ids:
            del gmap[tid]


async def add_tracks_fairly(
    player: wavelink.Player,
    tracks: list,
    user_id: int,
    enabled: bool = True,
):
    """Добавляет треки, чередуя их с чужими если fair_queue включено."""
    guild_id = player.guild.id
    for t in tracks:
        tag_track(guild_id, t, user_id)

    if not enabled or len(tracks) <= 1:
        for t in tracks:
            await player.queue.put_wait(t)
        return

    existing = list(player.queue)
    foreign_tracks = [
        t for t in existing if get_track_owner(guild_id, t) != user_id
    ]

    # Если чужих треков < 2 — не чередуем, просто добавляем в конец
    if len(foreign_tracks) < 2:
        for t in tracks:
            await player.queue.put_wait(t)
        return

    my_existing = [t for t in existing if get_track_owner(guild_id, t) == user_id]

    new_queue = []
    max_len = max(len(foreign_tracks), len(tracks))
    fi, ni = 0, 0
    for _ in range(max_len):
        if fi < len(foreign_tracks):
            new_queue.append(foreign_tracks[fi])
            fi += 1
        if ni < len(tracks):
            new_queue.append(tracks[ni])
            ni += 1
    new_queue.extend(my_existing)

    player.queue.clear()
    for t in new_queue:
        await player.queue.put_wait(t)
    cleanup_track_map(guild_id, new_queue)


async def get_fair_queue_enabled(guild_id: int) -> bool:
    if not db_pool:
        return False
    settings = await db_get_settings(guild_id)
    return settings.get("fair_queue", False)


# ─────────────────────────────────────────────
#  Spotify (fallback)
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
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
            ) as resp:
                if resp.status != 200:
                    log.warning("Spotify auth failed: %s", resp.status)
                    return None
                data = await resp.json()
                _spotify_token = data["access_token"]
                _spotify_token_expires = time.time() + data["expires_in"] - 60
                return _spotify_token
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Spotify auth error: %s", e)
        return None


def parse_spotify_url(url: str) -> Optional[tuple]:
    m = re.search(r'spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)', url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'spotify:(track|album|playlist):([A-Za-z0-9]+)', url)
    if m:
        return m.group(1), m.group(2)
    return None


async def fetch_spotify_tracks(url: str) -> Optional[list]:
    parsed = parse_spotify_url(url)
    if not parsed:
        return None
    sp_type, sp_id = parsed
    token = await get_spotify_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    tracks = []
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as s:
            if sp_type == "track":
                async with s.get(
                    f"https://api.spotify.com/v1/tracks/{sp_id}", headers=headers
                ) as r:
                    if r.status != 200:
                        return None
                    d = await r.json()
                    if d.get("artists") and d.get("name"):
                        tracks.append({"title": d["name"], "artist": d["artists"][0]["name"]})
            elif sp_type == "album":
                async with s.get(
                    f"https://api.spotify.com/v1/albums/{sp_id}/tracks?limit=50",
                    headers=headers,
                ) as r:
                    if r.status != 200:
                        return None
                    d = await r.json()
                    for item in d.get("items", [])[:SPOTIFY_TRACK_LIMIT]:
                        if item and item.get("name") and item.get("artists"):
                            tracks.append({
                                "title": item["name"],
                                "artist": item["artists"][0]["name"],
                            })
            elif sp_type == "playlist":
                async with s.get(
                    f"https://api.spotify.com/v1/playlists/{sp_id}/tracks?limit=50",
                    headers=headers,
                ) as r:
                    if r.status != 200:
                        return None
                    d = await r.json()
                    for item in d.get("items", [])[:SPOTIFY_TRACK_LIMIT]:
                        t = item.get("track") if item else None
                        if t and t.get("name") and t.get("artists"):
                            tracks.append({
                                "title": t["name"],
                                "artist": t["artists"][0]["name"],
                            })
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Spotify fetch error: %s", e)
        return None
    return tracks or None


# ─────────────────────────────────────────────
#  Яндекс.Музыка (без токена)
# ─────────────────────────────────────────────
_ym_client = None


async def get_ym_client():
    """Ленивая инициализация клиента Я.М без токена."""
    global _ym_client
    if not YANDEX_MUSIC_AVAILABLE:
        return None
    if _ym_client is not None:
        return _ym_client
    try:
        _ym_client = await YMClient().init()
        log.info("Yandex.Music клиент инициализирован")
        return _ym_client
    except Exception as e:
        log.warning("Yandex.Music init error: %s", e)
        return None


def parse_yandex_music_url(url: str) -> Optional[dict]:
    """Парсит ссылку Я.М: track / album / playlist."""
    # Трек в альбоме: /album/12345/track/67890
    m = re.search(r'music\.yandex\.(?:ru|com|by|kz)/album/(\d+)/track/(\d+)', url)
    if m:
        return {"type": "track", "track_id": m.group(2), "album_id": m.group(1)}

    # Короткий трек: /track/67890
    m = re.search(r'music\.yandex\.(?:ru|com|by|kz)/track/(\d+)', url)
    if m:
        return {"type": "track", "track_id": m.group(1)}

    # Альбом: /album/12345
    m = re.search(r'music\.yandex\.(?:ru|com|by|kz)/album/(\d+)(?:$|[/?#])', url)
    if m:
        return {"type": "album", "album_id": m.group(1)}

    # Плейлист: /users/USERNAME/playlists/3
    m = re.search(r'music\.yandex\.(?:ru|com|by|kz)/users/([^/]+)/playlists/(\d+)', url)
    if m:
        return {"type": "playlist", "owner": m.group(1), "kind": m.group(2)}

    return None


async def fetch_yandex_tracks(url: str) -> Optional[list]:
    """Получает метаданные треков из Я.М. Возвращает [{title, artist}]."""
    parsed = parse_yandex_music_url(url)
    if not parsed:
        return None

    client = await get_ym_client()
    if not client:
        return None

    tracks = []
    try:
        if parsed["type"] == "track":
            track_id = parsed["track_id"]
            result = await client.tracks([track_id])
            if result and len(result) > 0:
                t = result[0]
                if t and t.title and t.artists:
                    tracks.append({
                        "title": t.title,
                        "artist": t.artists[0].name,
                    })

        elif parsed["type"] == "album":
            album = await client.albums_with_tracks(parsed["album_id"])
            if album and album.volumes:
                all_tracks = []
                for volume in album.volumes:
                    all_tracks.extend(volume)
                for t in all_tracks[:SPOTIFY_TRACK_LIMIT]:
                    if t and t.title and t.artists:
                        tracks.append({
                            "title": t.title,
                            "artist": t.artists[0].name,
                        })

        elif parsed["type"] == "playlist":
            playlist = await client.users_playlists(
                kind=parsed["kind"], user_id=parsed["owner"]
            )
            if playlist and playlist.tracks:
                for short_track in playlist.tracks[:SPOTIFY_TRACK_LIMIT]:
                    t = short_track.track
                    if t and t.title and t.artists:
                        tracks.append({
                            "title": t.title,
                            "artist": t.artists[0].name,
                        })

    except Exception as e:
        log.warning("Yandex.Music fetch error: %s", e)
        return None

    return tracks or None


# ─────────────────────────────────────────────
#  Утилиты
# ─────────────────────────────────────────────
def format_duration(ms: int) -> str:
    seconds = ms // 1000
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def make_progress_bar(position_ms: int, length_ms: int, bar_len: int = 12) -> str:
    if length_ms <= 0:
        return "📻 Прямой эфир"
    progress = max(0.0, min(1.0, position_ms / length_ms))
    filled = int(progress * bar_len)
    bar = "━" * filled + "🔘" + "─" * (bar_len - filled)
    return f"{format_duration(position_ms)} {bar} {format_duration(length_ms)}"


def add_to_history(guild_id: int, track):
    if guild_id not in track_history:
        track_history[guild_id] = []
    track_history[guild_id].insert(0, {
        "title":  track.title,
        "uri":    track.uri,
        "length": track.length,
    })
    track_history[guild_id] = track_history[guild_id][:HISTORY_LIMIT]


async def apply_effect(player: wavelink.Player, effect: str):
    filters = wavelink.Filters()
    if effect == "bassboost":
        bands = [
            {"band": 0, "gain": 0.3},
            {"band": 1, "gain": 0.25},
            {"band": 2, "gain": 0.2},
            {"band": 3, "gain": 0.1},
        ]
        filters.equalizer.set(bands=bands)
    elif effect == "nightcore":
        filters.timescale.set(pitch=1.3, speed=1.2, rate=1.0)
    elif effect == "vaporwave":
        filters.timescale.set(pitch=0.8, speed=0.85, rate=1.0)
    elif effect == "slowmo":
        filters.timescale.set(pitch=1.0, speed=0.75, rate=1.0)
    elif effect == "8d":
        filters.rotation.set(rotation_hz=0.2)
    await player.set_filters(filters)


async def start_idle_timer(guild: discord.Guild, channel: discord.TextChannel):
    if guild.id in idle_tasks:
        idle_tasks[guild.id].cancel()

    async def _timer():
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        player: wavelink.Player = guild.voice_client
        if player and not player.playing and not player.paused:
            clear_player_state(guild.id)
            await player.disconnect()
            idle_tasks.pop(guild.id, None)
            try:
                await channel.send(f"💤 Вышел — {IDLE_TIMEOUT // 60} мин тишины.")
            except discord.HTTPException:
                pass

    idle_tasks[guild.id] = asyncio.create_task(_timer())


def cancel_idle_timer(guild_id: int):
    task = idle_tasks.pop(guild_id, None)
    if task:
        task.cancel()


def cancel_empty_channel_timer(guild_id: int):
    task = empty_channel_tasks.pop(guild_id, None)
    if task:
        task.cancel()


def is_birthday_today(day: int, month: int) -> bool:
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    moscow_time = now_utc + datetime.timedelta(hours=3)
    today = moscow_time.date()
    return today.day == day and today.month == month


async def full_disconnect(guild: discord.Guild):
    player: wavelink.Player = guild.voice_client
    cancel_idle_timer(guild.id)
    cancel_empty_channel_timer(guild.id)
    current_effect.pop(guild.id, None)
    reset_track_counts(guild.id)
    active_votes.pop(guild.id, None)
    track_user_map.pop(guild.id, None)
    clear_player_state(guild.id)
    if player:
        try:
            player.queue.clear()
        except Exception:
            pass
        try:
            await player.disconnect()
        except Exception as e:
            log.debug("Disconnect error: %s", e)


# ─────────────────────────────────────────────
#  LYRICS: LRClib → Genius → lyrics.ovh
# ─────────────────────────────────────────────
def _clean_title(title: str) -> tuple[str, str]:
    parts = re.split(r'\s*[-–—|]\s*', title, maxsplit=1)
    if len(parts) == 2:
        artist, song = parts[0].strip(), parts[1].strip()
    else:
        artist, song = "", title.strip()
    song = re.sub(r'\(.*?\)|\[.*?\]|\{.*?\}', '', song).strip()
    song = re.sub(r'\s*(feat\.?|ft\.?|featuring|prod\.?(\sby)?|w[/\\])\s+.*$',
                  '', song, flags=re.IGNORECASE).strip()
    song = re.sub(r'\b(official|lyric|audio|video|music|mv|hd|4k|hq)\s*(video|audio)?\b.*$',
                  '', song, flags=re.IGNORECASE).strip()
    artist = re.sub(r'\s*(feat\.?|ft\.?|featuring|x|&)\s+.*$', '', artist,
                    flags=re.IGNORECASE).strip()
    return artist, song


async def _fetch_lrclib(title: str, duration_ms: Optional[int] = None) -> Optional[dict]:
    artist, song = _clean_title(title)
    if not song:
        return None

    log.info("LRClib search: artist=%r song=%r", artist, song)
    timeout = aiohttp.ClientTimeout(total=8)
    headers = {"User-Agent": f"{BOT_NAME}Bot/1.0 (Discord music bot)"}

    async def _try_request(url: str, params: dict) -> Optional[object]:
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(url, params=params) as r:
                    if r.status != 200:
                        return None
                    return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("LRClib request error: %s", e)
            return None

    # 1. Точное совпадение по duration
    if artist and duration_ms and duration_ms > 0:
        params = {
            "track_name": song,
            "artist_name": artist,
            "duration": duration_ms // 1000,
        }
        result = await _try_request("https://lrclib.net/api/get", params)
        if isinstance(result, dict) and (result.get("plainLyrics") or result.get("syncedLyrics")):
            log.info("LRClib: нашли через get (exact match)")
            return {
                "plain": result.get("plainLyrics"),
                "synced": result.get("syncedLyrics"),
            }

    # 2. Поиск
    query = f"{artist} {song}".strip() if artist else song
    results = await _try_request("https://lrclib.net/api/search", {"q": query})
    if not isinstance(results, list) or len(results) == 0:
        log.info("LRClib: ничего не найдено")
        return None

    for item in results[:5]:
        plain = item.get("plainLyrics")
        synced = item.get("syncedLyrics")
        if plain or synced:
            log.info("LRClib: нашли через search (%s)", item.get("trackName"))
            return {"plain": plain, "synced": synced}
    log.info("LRClib: все результаты без текста")
    return None


def _is_bad_genius_hit(hit: dict) -> bool:
    result = hit.get("result", {})
    url = result.get("url", "").lower()
    title = result.get("title", "").lower()
    bad_keywords = [
        "перевод", "translation", "traducción", "tradução",
        "genius-users", "genius-romanizations",
        "annotated", "q-and-a", "interview",
    ]
    for kw in bad_keywords:
        if kw in url or kw in title:
            return True
    if result.get("_type") and result["_type"] != "song":
        return True
    return False


async def _fetch_genius(title: str) -> Optional[str]:
    if not GENIUS_TOKEN:
        return None
    artist, song = _clean_title(title)
    query = f"{artist} {song}".strip() if artist else song
    if not query:
        return None

    log.info("Genius search: %r", query)
    headers = {"Authorization": f"Bearer {GENIUS_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(
                "https://api.genius.com/search",
                params={"q": query},
            ) as r:
                if r.status == 401:
                    log.warning("Genius: невалидный токен (401)")
                    return None
                if r.status != 200:
                    return None
                data = await r.json()
                hits = data.get("response", {}).get("hits", [])

            if not hits:
                return None

            song_url = None
            for hit in hits:
                if _is_bad_genius_hit(hit):
                    continue
                song_url = hit["result"]["url"]
                break
            if not song_url:
                return None

            async with s.get(
                song_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            ) as r:
                if r.status != 200:
                    return None
                html = await r.text()
        return _parse_genius_html(html)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.debug("Genius network error: %s", e)
        return None
    except Exception as e:
        log.error("Genius unexpected error: %s", e, exc_info=True)
        return None


def _parse_genius_html(html: str) -> Optional[str]:
    import html as html_module
    containers = re.findall(
        r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)(?=<div[^>]*data-lyrics-container|</section)',
        html, re.DOTALL,
    )
    if not containers:
        containers = re.findall(
            r'<div[^>]*class="[^"]*Lyrics__Container[^"]*"[^>]*>(.*?)</div>',
            html, re.DOTALL,
        )
    if not containers:
        return None
    combined = "\n".join(containers)
    combined = re.sub(r'<a[^>]*class="[^"]*ReferentFragment[^"]*"[^>]*>(.*?)</a>',
                      r'\1', combined, flags=re.DOTALL)
    combined = re.sub(r'<br\s*/?>', '\n', combined)
    combined = re.sub(r'<[^>]+>', '', combined)
    combined = html_module.unescape(combined)
    combined = re.sub(r'\n{3,}', '\n\n', combined).strip()
    if len(combined) < 50:
        return None
    return combined or None


async def _fetch_lyricsovh(title: str) -> Optional[str]:
    artist, song = _clean_title(title)
    if not song:
        return None
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            if artist:
                try:
                    url = f"https://api.lyrics.ovh/v1/{artist}/{song}"
                    async with s.get(url) as r:
                        if r.status == 200:
                            data = await r.json()
                            lyrics = data.get("lyrics")
                            if lyrics and lyrics.strip():
                                return lyrics
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
    except Exception:
        pass
    return None


async def fetch_lyrics(title: str, duration_ms: Optional[int] = None) -> Optional[dict]:
    """Возвращает dict: {'text': ..., 'source': ..., 'synced': bool} либо None."""
    # 1. LRClib
    result = await _fetch_lrclib(title, duration_ms)
    if result:
        text = result.get("plain")
        if not text and result.get("synced"):
            text = re.sub(r'\[\d+:\d+\.\d+\]\s*', '', result["synced"]).strip()
        if text:
            return {
                "text": text,
                "source": "LRClib",
                "synced": bool(result.get("synced")),
            }

    # 2. Genius
    text = await _fetch_genius(title)
    if text:
        return {"text": text, "source": "Genius", "synced": False}

    # 3. lyrics.ovh
    text = await _fetch_lyricsovh(title)
    if text:
        return {"text": text, "source": "lyrics.ovh", "synced": False}

    return None


# ─────────────────────────────────────────────
#  Голосование за скип
# ─────────────────────────────────────────────
class VoteSkipView(discord.ui.View):
    def __init__(self, guild: discord.Guild, channel: discord.TextChannel,
                 required_percent: int, initiator: discord.Member):
        super().__init__(timeout=30)
        self.guild = guild
        self.channel = channel
        self.required_percent = required_percent
        self.initiator = initiator
        self.votes_yes: set[int] = {initiator.id}
        self.votes_no: set[int] = set()
        self.message: Optional[discord.Message] = None
        self.resolved = False

    def get_voter_count(self) -> int:
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.channel:
            return 1
        return max(1, len([m for m in player.channel.members if not m.bot]))

    def is_passed(self) -> bool:
        total = self.get_voter_count()
        needed = max(1, int(total * self.required_percent / 100))
        return len(self.votes_yes) >= needed

    def status_text(self) -> str:
        total = self.get_voter_count()
        needed = max(1, int(total * self.required_percent / 100))
        return (
            f"⏭ **Голосование за скип**\n"
            f"✅ За: **{len(self.votes_yes)}** | ❌ Против: **{len(self.votes_no)}**\n"
            f"Нужно **{needed}** из **{total}** голосов | Осталось 30 сек"
        )

    @discord.ui.button(label="✅ За скип", style=discord.ButtonStyle.success)
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.channel or interaction.user not in player.channel.members:
            await interaction.response.send_message(
                "❗ Войди в голосовой канал чтобы голосовать.", ephemeral=True
            )
            return
        self.votes_yes.add(interaction.user.id)
        self.votes_no.discard(interaction.user.id)
        await interaction.response.edit_message(content=self.status_text(), view=self)
        if self.is_passed():
            await self.do_skip()

    @discord.ui.button(label="❌ Против", style=discord.ButtonStyle.danger)
    async def vote_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.channel or interaction.user not in player.channel.members:
            await interaction.response.send_message(
                "❗ Войди в голосовой канал чтобы голосовать.", ephemeral=True
            )
            return
        self.votes_no.add(interaction.user.id)
        self.votes_yes.discard(interaction.user.id)
        await interaction.response.edit_message(content=self.status_text(), view=self)

    async def do_skip(self):
        if self.resolved:
            return
        self.resolved = True
        self.stop()
        active_votes.pop(self.guild.id, None)
        player: wavelink.Player = self.guild.voice_client
        if player and (player.playing or player.paused):
            try:
                if player.queue.mode == wavelink.QueueMode.loop:
                    player.queue.mode = wavelink.QueueMode.normal
                await player.skip(force=True)
            except Exception as e:
                log.warning("Skip error: %s", e)
        if self.message:
            try:
                await self.message.edit(content="✅ Голосование прошло — трек пропущен!", view=None)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        if self.resolved:
            return
        self.resolved = True
        active_votes.pop(self.guild.id, None)
        if self.message:
            try:
                await self.message.edit(
                    content=f"❌ Голосование завершилось — недостаточно голосов "
                            f"({len(self.votes_yes)} за, нужно было больше).",
                    view=None
                )
            except discord.HTTPException:
                pass


# ─────────────────────────────────────────────
#  Пагинация очереди
# ─────────────────────────────────────────────
class JumpToPageModal(discord.ui.Modal, title="Перейти к странице"):
    page_input = discord.ui.TextInput(
        label="Номер страницы",
        placeholder="Введи число",
        required=True,
        max_length=4,
    )

    def __init__(self, view: "QueuePaginationView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = int((self.page_input.value or "").strip())
        except ValueError:
            await interaction.response.send_message("❗ Введи число.", ephemeral=True)
            return
        total_pages = self.view_ref.total_pages()
        if target < 1 or target > total_pages:
            await interaction.response.send_message(
                f"❗ Номер от 1 до {total_pages}.", ephemeral=True
            )
            return
        self.view_ref.current_page = target - 1
        await interaction.response.edit_message(
            content=self.view_ref.build_text(),
            view=self.view_ref,
        )


class QueuePaginationView(discord.ui.View):
    def __init__(self, guild: discord.Guild, user_id: int):
        super().__init__(timeout=180)
        self.guild = guild
        self.user_id = user_id
        self.current_page = 0
        self.message: Optional[discord.Message] = None

    @property
    def player(self) -> Optional[wavelink.Player]:
        return self.guild.voice_client

    def get_queue_snapshot(self) -> list:
        p = self.player
        if not p:
            return []
        return list(p.queue)

    def total_pages(self) -> int:
        snapshot = self.get_queue_snapshot()
        if not snapshot:
            return 1
        return max(1, (len(snapshot) + TRACKS_PER_PAGE - 1) // TRACKS_PER_PAGE)

    def build_text(self) -> str:
        p = self.player
        snapshot = self.get_queue_snapshot()
        total_pages = self.total_pages()
        self.current_page = max(0, min(self.current_page, total_pages - 1))

        lines = []
        if p and p.current:
            t = p.current
            link = f" — [открыть]({t.uri})" if t.uri else ""
            lines.append(
                f"🎵 **Сейчас:** {t.title} `[{format_duration(t.length)}]`{link}\n"
            )

        if not snapshot:
            lines.append("📭 Очередь пуста.")
            self._update_buttons(total_pages)
            return "\n".join(lines)

        start = self.current_page * TRACKS_PER_PAGE
        end = start + TRACKS_PER_PAGE
        shown = snapshot[start:end]

        lines.append(
            f"**Очередь** — `{len(snapshot)} треков` · "
            f"Страница **{self.current_page + 1}/{total_pages}**\n"
        )
        for i, t in enumerate(shown, start=start + 1):
            link = f" — [открыть]({t.uri})" if t.uri else ""
            lines.append(f"`{i}.` {t.title} `[{format_duration(t.length)}]`{link}")

        self._update_buttons(total_pages)
        return "\n".join(lines)

    def _update_buttons(self, total_pages: int):
        self.first_btn.disabled = self.current_page == 0
        self.prev_btn.disabled = self.current_page == 0
        self.next_btn.disabled = self.current_page >= total_pages - 1
        self.last_btn.disabled = self.current_page >= total_pages - 1
        self.jump_btn.disabled = total_pages <= 1

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await interaction.response.edit_message(content=self.build_text(), view=self)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(content=self.build_text(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.total_pages() - 1, self.current_page + 1)
        await interaction.response.edit_message(content=self.build_text(), view=self)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = self.total_pages() - 1
        await interaction.response.edit_message(content=self.build_text(), view=self)

    @discord.ui.button(label="🔢 К странице", style=discord.ButtonStyle.success)
    async def jump_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(JumpToPageModal(self))


# ─────────────────────────────────────────────
#  Модалка добавления в плейлист
# ─────────────────────────────────────────────
class AddToPlaylistModal(discord.ui.Modal, title="Добавить в плейлист"):
    playlist_name = discord.ui.TextInput(
        label="Название плейлиста",
        placeholder="Введи название плейлиста",
        required=True,
        max_length=PLAYLIST_NAME_MAX,
    )

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.current:
            await interaction.response.send_message("❗ Ничего не играет.", ephemeral=True)
            return
        if not db_pool:
            await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
            return
        name = (self.playlist_name.value or "").strip()
        if not name:
            await interaction.response.send_message("❗ Название не может быть пустым.", ephemeral=True)
            return
        playlist = await db_get_playlist(interaction.user.id, name)
        if not playlist:
            await interaction.response.send_message(
                f"❗ Плейлист **{name}** не найден.", ephemeral=True
            )
            return
        track = player.current
        await db_add_track(playlist["id"], track.title, track.uri or "", track.length)
        await interaction.response.send_message(
            f"✅ **{track.title}** добавлен в **{name}**!", ephemeral=True
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

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary, row=0, label="Пауза")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if p and p.playing and not p.paused:
            await p.pause(True)
            button.emoji = "▶️"
            button.label = "Играть"
            await interaction.response.edit_message(view=self)
        elif p and p.paused:
            await p.pause(False)
            button.emoji = "⏸"
            button.label = "Пауза"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, row=0, label="Скип")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if not p or not (p.playing or p.paused):
            await interaction.response.defer()
            return
        if db_pool:
            settings = await db_get_settings(self.guild.id)
            if settings.get("vote_skip_enabled") and not await is_dj(interaction.user):
                await interaction.response.defer()
                await start_vote_skip(
                    interaction.user, self.guild,
                    interaction.channel, settings.get("vote_skip_percent", 50)
                )
                return
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        if p.queue.mode == wavelink.QueueMode.loop:
            p.queue.mode = wavelink.QueueMode.normal
        await p.skip(force=True)
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0, label="Повтор")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
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

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0, label="Shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if p and len(p.queue) > 1:
            p.queue.shuffle()
            await interaction.response.send_message("🔀 Очередь перемешана.", ephemeral=True)
        else:
            await interaction.response.send_message("❗ Нечего перемешивать.", ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=0, label="Очередь")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if not p or (not p.current and p.queue.is_empty):
            await interaction.response.send_message("📭 Очередь пуста.", ephemeral=True)
            return
        view = QueuePaginationView(self.guild, interaction.user.id)
        text = view.build_text()
        await interaction.response.send_message(text, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1, label="-10%")
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        new_vol = max(0, p.volume - 10)
        await p.set_volume(new_vol)
        await interaction.response.send_message(f"🔉 Громкость: **{new_vol}%**", ephemeral=True)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1, label="+10%")
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        new_vol = min(100, p.volume + 10)
        await p.set_volume(new_vol)
        await interaction.response.send_message(f"🔊 Громкость: **{new_vol}%**", ephemeral=True)

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.secondary, row=1, label="В плейлист")
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddToPlaylistModal(self.guild))

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=1, label="Стоп")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        await full_disconnect(self.guild)
        await interaction.response.send_message("⏹ Остановлено.", ephemeral=True)


async def start_vote_skip(member: discord.Member, guild: discord.Guild,
                          channel: discord.TextChannel, percent: int):
    if guild.id in active_votes:
        view = active_votes[guild.id]
        view.votes_yes.add(member.id)
        view.votes_no.discard(member.id)
        if view.message:
            try:
                await view.message.edit(content=view.status_text(), view=view)
            except discord.HTTPException:
                pass
        if view.is_passed():
            await view.do_skip()
        return

    view = VoteSkipView(guild, channel, percent, member)
    active_votes[guild.id] = view
    try:
        msg = await channel.send(view.status_text(), view=view)
        view.message = msg
    except discord.HTTPException as e:
        log.warning("Vote skip send error: %s", e)
        active_votes.pop(guild.id, None)
        return

    if view.is_passed():
        await view.do_skip()


# ─────────────────────────────────────────────
#  View: выбор трека
# ─────────────────────────────────────────────
class TrackSelectView(discord.ui.View):
    def __init__(self, tracks: list, guild: discord.Guild,
                 voice_channel: discord.VoiceChannel,
                 text_channel: discord.TextChannel,
                 search_msg: discord.Message,
                 user_id: int):
        super().__init__(timeout=60)
        self.tracks = tracks
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.search_msg = search_msg
        self.user_id = user_id
        for i in range(len(tracks)):
            btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary)
            btn.callback = self._make_cb(i)
            self.add_item(btn)
        cancel = discord.ui.Button(label="✖ Отмена", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❗ Это меню выбора не для тебя.", ephemeral=True
            )
            return False
        return True

    def _make_cb(self, index: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            track = self.tracks[index]
            player: wavelink.Player = self.guild.voice_client
            try:
                if player is None:
                    player = await self.voice_channel.connect(cls=wavelink.Player)
                elif player.channel != self.voice_channel:
                    await player.move_to(self.voice_channel)
            except Exception as e:
                log.warning("Voice connect error: %s", e)
                try:
                    await self.search_msg.edit(content=f"❗ Не удалось подключиться: {e}", view=None)
                except discord.HTTPException:
                    pass
                self.stop()
                return
            player.autoplay = wavelink.AutoPlayMode.disabled
            get_player_state(self.guild.id)["text_channel_id"] = self.text_channel.id
            fair = await get_fair_queue_enabled(self.guild.id)
            if not player.playing:
                try:
                    await self.search_msg.delete()
                except discord.HTTPException:
                    pass
                increment_user_track_count(self.guild.id, self.user_id)
                tag_track(self.guild.id, track, self.user_id)
                await player.play(track)
            else:
                await add_tracks_fairly(player, [track], self.user_id, enabled=fair)
                increment_user_track_count(self.guild.id, self.user_id)
                try:
                    await self.search_msg.edit(
                        content=f"➕ **Добавлено:** {track.title} `[{format_duration(track.length)}]`",
                        view=None,
                    )
                except discord.HTTPException:
                    pass
            cancel_idle_timer(self.guild.id)
            self.stop()
        return callback

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self.search_msg.edit(content="❌ Отменено.", view=None)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self):
        try:
            await self.search_msg.edit(content="⏱ Время вышло.", view=None)
        except discord.HTTPException:
            pass


# ─────────────────────────────────────────────
#  События Wavelink
# ─────────────────────────────────────────────
@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player = payload.player
    if not player:
        return
    track = payload.track
    guild = player.guild
    state = get_player_state(guild.id)

    is_birthday_track = state.get("birthday_playing", False)

    add_to_history(guild.id, track)

    if db_pool:
        try:
            await db_increment_stats(guild.id, track.length)
        except Exception as e:
            log.warning("Stats increment error: %s", e)

    channel_id = state.get("text_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if not channel:
        return

    msg_id = state.get("now_playing_msg_id")
    if msg_id:
        try:
            old = await channel.fetch_message(msg_id)
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    loop_labels = {
        wavelink.QueueMode.normal:   "выкл ➡️",
        wavelink.QueueMode.loop:     "трек 🔂",
        wavelink.QueueMode.loop_all: "очередь 🔁",
    }
    effect = current_effect.get(guild.id, "off")
    effect_indicator = f" | ✨ **{EFFECTS[effect]}**" if effect != "off" else ""
    birthday_indicator = "\n🎂 **Поздравительный трек**" if is_birthday_track else ""

    link = f" — [открыть]({track.uri})" if track.uri else ""
    progress = make_progress_bar(0, track.length)
    text = (
        f"🎵 **Сейчас играет:** {track.title}{link}\n"
        f"`{progress}`\n"
        f"Повтор: **{loop_labels[player.queue.mode]}** | 🔊 **{player.volume}%**"
        f"{effect_indicator}{birthday_indicator}"
    )
    try:
        msg = await channel.send(text, view=PlayerControls(guild))
        state["now_playing_msg_id"] = msg.id
    except discord.HTTPException as e:
        log.warning("Now playing send error: %s", e)


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    if not player:
        return
    guild = player.guild
    state = get_player_state(guild.id)
    channel_id = state.get("text_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None

    if state.get("birthday_playing"):
        state["birthday_playing"] = False
        resume = state.pop("birthday_resume", None)
        if resume:
            try:
                state["birthday_seek_to"] = resume["position"]
            except Exception as e:
                log.warning("Birthday resume setup error: %s", e)

    seek_to = state.get("birthday_seek_to")
    if seek_to is not None and not player.queue.is_empty:
        async def _seek_after_start():
            await asyncio.sleep(0.8)
            p: wavelink.Player = guild.voice_client
            if p and p.current:
                try:
                    await p.seek(min(seek_to, p.current.length))
                except Exception as e:
                    log.debug("Birthday seek error: %s", e)
            state.pop("birthday_seek_to", None)
        asyncio.create_task(_seek_after_start())

    if player.queue.mode == wavelink.QueueMode.loop:
        try:
            await player.play(payload.track)
        except Exception as e:
            log.warning("Loop replay error: %s", e)
        return
    if player.queue.mode == wavelink.QueueMode.loop_all:
        try:
            await player.queue.put_wait(payload.track)
        except Exception:
            pass
    if not player.queue.is_empty:
        try:
            await player.play(player.queue.get())
        except Exception as e:
            log.warning("Next track play error: %s", e)
    elif channel:
        await start_idle_timer(guild, channel)


@bot.event
async def on_wavelink_inactive_player(player: wavelink.Player):
    if not player:
        return
    await full_disconnect(player.guild)


@bot.event
async def on_wavelink_node_ready(payload):
    log.info("Lavalink node ready: %s", getattr(payload, "node", "?"))


# ─────────────────────────────────────────────
#  День рождения
# ─────────────────────────────────────────────
async def _play_birthday_now(member: discord.Member,
                              channel: discord.VoiceChannel,
                              birthday: dict):
    song_query = birthday.get("birthday_song") or "Happy Birthday instrumental"
    try:
        results = await wavelink.Playable.search(
            song_query, source=wavelink.TrackSource.YouTube
        )
    except Exception as e:
        log.warning("Birthday search error: %s", e)
        return
    if not results:
        return
    bday_track = results[0] if isinstance(results, list) else results.tracks[0]

    player: wavelink.Player = member.guild.voice_client
    state = get_player_state(member.guild.id)

    text_channel = None
    if state.get("text_channel_id"):
        text_channel = member.guild.get_channel(state["text_channel_id"])
    if not text_channel:
        text_channel = member.guild.system_channel
    if not text_channel:
        for ch in member.guild.text_channels:
            if ch.permissions_for(member.guild.me).send_messages:
                text_channel = ch
                break

    if player is None or not player.connected:
        try:
            player = await channel.connect(cls=wavelink.Player)
            player.autoplay = wavelink.AutoPlayMode.disabled
            if text_channel:
                state["text_channel_id"] = text_channel.id
            state["birthday_playing"] = True
            await player.play(bday_track)
            if text_channel:
                try:
                    await text_channel.send(
                        f"🎂 С Днём рождения, {member.mention}! "
                        f"🎉 {BOT_NAME} поздравляет тебя!"
                    )
                except discord.HTTPException:
                    pass
        except Exception as e:
            log.warning("Birthday play error: %s", e)
        return

    if player.channel != channel:
        return

    current_track = player.current
    if current_track is None:
        await player.queue.put_wait(bday_track)
        if not player.playing:
            await player.play(player.queue.get())
        return

    state["birthday_resume"] = {
        "uri": current_track.uri,
        "title": current_track.title,
        "position": player.position,
    }
    state["birthday_playing"] = True

    prev_mode = player.queue.mode
    if prev_mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal

    try:
        resume_results = await wavelink.Playable.search(
            current_track.uri or current_track.title,
            source=wavelink.TrackSource.YouTube,
        )
        resume_track = None
        if resume_results:
            resume_track = resume_results[0] if isinstance(resume_results, list) \
                else resume_results.tracks[0]

        try:
            player.queue.put_at(0, bday_track)
            if resume_track:
                player.queue.put_at(1, resume_track)
        except AttributeError:
            current_queue = list(player.queue)
            player.queue.clear()
            await player.queue.put_wait(bday_track)
            if resume_track:
                await player.queue.put_wait(resume_track)
            for t in current_queue:
                await player.queue.put_wait(t)

        state["birthday_prev_mode"] = prev_mode
        await player.skip(force=True)

        if text_channel:
            try:
                await text_channel.send(
                    f"🎂 С Днём рождения, {member.mention}! "
                    f"🎉 {BOT_NAME} прерывает музыку ради поздравления!"
                )
            except discord.HTTPException:
                pass
    except Exception as e:
        log.warning("Birthday interrupt error: %s", e)
        state.pop("birthday_resume", None)
        state["birthday_playing"] = False


async def _handle_birthday(member: discord.Member, channel: discord.VoiceChannel):
    if not db_pool:
        return
    try:
        birthday = await db_get_birthday(member.id)
    except Exception as e:
        log.warning("Birthday fetch error: %s", e)
        return
    if not birthday or not is_birthday_today(birthday["birth_day"], birthday["birth_month"]):
        return
    state = get_player_state(member.guild.id)
    already = state.setdefault("birthday_greeted", set())
    if member.id in already:
        return
    already.add(member.id)
    await _play_birthday_now(member, channel, birthday)


@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
    if member.bot:
        return

    if after.channel and not before.channel:
        await _handle_birthday(member, after.channel)

    player: wavelink.Player = member.guild.voice_client
    if not player or not player.channel:
        return
    if after.channel and after.channel == player.channel:
        cancel_idle_timer(member.guild.id)
        cancel_empty_channel_timer(member.guild.id)
        return

    non_bots = [m for m in player.channel.members if not m.bot]
    if len(non_bots) > 0:
        return

    cancel_empty_channel_timer(member.guild.id)

    async def _empty_timer(guild: discord.Guild):
        try:
            await asyncio.sleep(EMPTY_CH_TIMEOUT)
        except asyncio.CancelledError:
            return
        p: wavelink.Player = guild.voice_client
        if not p or not p.connected or not p.channel:
            empty_channel_tasks.pop(guild.id, None)
            return
        current_non_bots = [m for m in p.channel.members if not m.bot]
        if len(current_non_bots) > 0:
            empty_channel_tasks.pop(guild.id, None)
            return
        state = get_player_state(guild.id)
        channel_id = state.get("text_channel_id")
        await full_disconnect(guild)
        if channel_id:
            ch = guild.get_channel(channel_id)
            if ch:
                try:
                    await ch.send("👋 Все ушли — выхожу из канала.")
                except discord.HTTPException:
                    pass
        empty_channel_tasks.pop(guild.id, None)

    empty_channel_tasks[member.guild.id] = asyncio.create_task(_empty_timer(member.guild))


# ─────────────────────────────────────────────
#  Определение источника
# ─────────────────────────────────────────────
def detect_source_from_url(query: str) -> wavelink.TrackSource:
    q = query.lower()
    if "soundcloud.com" in q or q.startswith("sc:"):
        return wavelink.TrackSource.SoundCloud
    return wavelink.TrackSource.YouTube


async def ensure_voice_connection(
    interaction: discord.Interaction,
) -> Optional[wavelink.Player]:
    if not interaction.user.voice or not interaction.user.voice.channel:
        return None
    player: wavelink.Player = interaction.guild.voice_client
    try:
        if player is None:
            player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
        elif player.channel != interaction.user.voice.channel:
            await player.move_to(interaction.user.voice.channel)
    except Exception as e:
        log.warning("Voice connect error: %s", e)
        return None
    player.autoplay = wavelink.AutoPlayMode.disabled
    get_player_state(interaction.guild_id)["text_channel_id"] = interaction.channel.id
    cancel_idle_timer(interaction.guild_id)
    cancel_empty_channel_timer(interaction.guild_id)
    return player


# ─────────────────────────────────────────────
#  Slash-команды: воспроизведение
# ─────────────────────────────────────────────
@tree.command(name="play", description="Поиск трека или добавление плейлиста")
@app_commands.describe(query="Название, ссылка YouTube/SoundCloud/Spotify или плейлист")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not interaction.user.voice:
        await interaction.followup.send("❗ Зайди в голосовой канал сначала.")
        return

    msg = await interaction.followup.send(f"🔍 Ищу **{query[:100]}**...", wait=True)

    source = detect_source_from_url(query)

    try:
        results = await wavelink.Playable.search(query, source=source)
    except Exception as e:
        log.warning("Search error: %s", e)
        results = None

    # Яндекс.Музыка — свой обработчик (Lavalink не поддерживает Я.М)
    if not results and "music.yandex." in query.lower():
        if not YANDEX_MUSIC_AVAILABLE:
            await msg.edit(
                content="❗ Поддержка Яндекс.Музыки не установлена.\n"
                        "_Обратись к администратору бота._"
            )
            return
        await msg.edit(content="🎵 Получаю треки из Яндекс.Музыки...")
        ym_tracks = await fetch_yandex_tracks(query)
        if not ym_tracks:
            await msg.edit(
                content="❗ Не удалось получить треки из Я.М.\n"
                        "_Возможно, ссылка неверная или трек недоступен._"
            )
            return

        limited_ym = ym_tracks[:PLAYLIST_TRACK_LIMIT]
        if not await check_track_limit(interaction, len(limited_ym)):
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            return
        player = await ensure_voice_connection(interaction)
        if player is None:
            await msg.edit(content="❗ Не удалось подключиться к голосовому каналу.")
            return

        if len(limited_ym) == 1:
            await msg.edit(content=f"🔍 Ищу трек на YouTube...")
        else:
            await msg.edit(content=f"🔍 Ищу {len(limited_ym)} треков на YouTube...")

        added_tracks = []
        for ym in limited_ym:
            try:
                yt_results = await wavelink.Playable.search(
                    f"{ym['artist']} - {ym['title']}",
                    source=wavelink.TrackSource.YouTube,
                )
                if yt_results:
                    track = yt_results[0] if isinstance(yt_results, list) \
                        else yt_results.tracks[0]
                    added_tracks.append(track)
            except Exception as e:
                log.debug("Yandex->YT search error: %s", e)
                continue

        if not added_tracks:
            await msg.edit(content="😕 Не удалось найти треки на YouTube.")
            return

        fair = await get_fair_queue_enabled(interaction.guild_id)
        if not player.playing:
            first_track = added_tracks[0]
            rest = added_tracks[1:]
            tag_track(interaction.guild_id, first_track, interaction.user.id)
            for t in rest:
                tag_track(interaction.guild_id, t, interaction.user.id)
            for t in rest:
                await player.queue.put_wait(t)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            await player.play(first_track)
        else:
            await add_tracks_fairly(
                player, added_tracks, interaction.user.id, enabled=fair
            )
            if len(added_tracks) == 1:
                await msg.edit(
                    content=f"➕ **Добавлено из Я.М:** {added_tracks[0].title} "
                            f"`[{format_duration(added_tracks[0].length)}]`",
                    view=None,
                )
            else:
                await msg.edit(
                    content=f"📋 Добавлено из Яндекс.Музыки: `{len(added_tracks)} треков`"
                )
        increment_user_track_count(
            interaction.guild_id, interaction.user.id, len(added_tracks)
        )
        return

    # Fallback на Spotify API
    if not results and "spotify.com" in query.lower():
        await msg.edit(content="🎵 Получаю треки из Spotify через API...")
        spotify_tracks = await fetch_spotify_tracks(query)
        if not spotify_tracks:
            await msg.edit(content="❗ Не удалось получить треки Spotify. Проверь ссылку.")
            return

        limited_sp = spotify_tracks[:PLAYLIST_TRACK_LIMIT]
        if not await check_track_limit(interaction, len(limited_sp)):
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            return
        player = await ensure_voice_connection(interaction)
        if player is None:
            await msg.edit(content="❗ Не удалось подключиться к голосовому каналу.")
            return
        await msg.edit(content=f"🔍 Ищу {len(limited_sp)} треков на YouTube...")
        added_tracks = []
        for sp in limited_sp:
            try:
                sp_results = await wavelink.Playable.search(
                    f"{sp['artist']} - {sp['title']}", source=wavelink.TrackSource.YouTube
                )
                if sp_results:
                    track = sp_results[0] if isinstance(sp_results, list) else sp_results.tracks[0]
                    added_tracks.append(track)
            except Exception as e:
                log.debug("Spotify->YT search error: %s", e)
                continue
        if not added_tracks:
            await msg.edit(content="😕 Не удалось найти треки на YouTube.")
            return

        fair = await get_fair_queue_enabled(interaction.guild_id)
        if not player.playing:
            # Первый трек — сразу играть, остальные в очередь
            first_track = added_tracks[0]
            rest = added_tracks[1:]
            tag_track(interaction.guild_id, first_track, interaction.user.id)
            for t in rest:
                tag_track(interaction.guild_id, t, interaction.user.id)
            for t in rest:
                await player.queue.put_wait(t)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            await player.play(first_track)
        else:
            await add_tracks_fairly(player, added_tracks, interaction.user.id, enabled=fair)
            await msg.edit(content=f"📋 Добавлено из Spotify: `{len(added_tracks)} треков`")
        increment_user_track_count(interaction.guild_id, interaction.user.id, len(added_tracks))
        return

    if not results:
        await msg.edit(content="😕 Ничего не найдено.")
        return

    # Плейлист
    if isinstance(results, wavelink.Playlist):
        limited = results.tracks[:PLAYLIST_TRACK_LIMIT]
        if not await check_track_limit(interaction, len(limited)):
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            return
        player = await ensure_voice_connection(interaction)
        if player is None:
            await msg.edit(content="❗ Не удалось подключиться к голосовому каналу.")
            return

        fair = await get_fair_queue_enabled(interaction.guild_id)
        count = len(limited)
        total = len(results.tracks)
        suffix = f" (лимит {PLAYLIST_TRACK_LIMIT})" if total > PLAYLIST_TRACK_LIMIT else ""

        if not player.playing:
            first_track = limited[0]
            rest = limited[1:]
            tag_track(interaction.guild_id, first_track, interaction.user.id)
            for t in rest:
                tag_track(interaction.guild_id, t, interaction.user.id)
            for t in rest:
                await player.queue.put_wait(t)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            await player.play(first_track)
        else:
            await add_tracks_fairly(player, limited, interaction.user.id, enabled=fair)
            await msg.edit(
                content=f"📋 **Плейлист добавлен:** {results.name} — `{count} треков`{suffix}",
                view=None,
            )
        increment_user_track_count(interaction.guild_id, interaction.user.id, count)
        return

    # Одиночная ссылка
    if query.startswith("http") and len(results) == 1:
        if not await check_track_limit(interaction):
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            return
        player = await ensure_voice_connection(interaction)
        if player is None:
            await msg.edit(content="❗ Не удалось подключиться к голосовому каналу.")
            return
        track = results[0]
        fair = await get_fair_queue_enabled(interaction.guild_id)
        if not player.playing:
            tag_track(interaction.guild_id, track, interaction.user.id)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            await player.play(track)
        else:
            await add_tracks_fairly(player, [track], interaction.user.id, enabled=fair)
            await msg.edit(
                content=f"➕ **Добавлено:** {track.title} `[{format_duration(track.length)}]`",
                view=None,
            )
        increment_user_track_count(interaction.guild_id, interaction.user.id)
        return

    # Обычный поиск
    if not await check_track_limit(interaction):
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        return

    tracks = results[:5]
    lines = ["**Результаты поиска:**\n"]
    for i, t in enumerate(tracks, 1):
        lines.append(f"`{i}.` {t.title} `[{format_duration(t.length)}]`")
    lines.append("\nВыбери трек кнопкой:")
    view = TrackSelectView(
        tracks, interaction.guild,
        interaction.user.voice.channel, interaction.channel, msg,
        interaction.user.id
    )
    await msg.edit(content="\n".join(lines), view=view)


@tree.command(name="savequeue", description="Сохранить текущую очередь как плейлист")
@app_commands.describe(name="Название нового плейлиста")
async def savequeue_cmd(interaction: discord.Interaction, name: str):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    name = (name or "").strip()
    if not name:
        await interaction.response.send_message("❗ Название не может быть пустым.", ephemeral=True)
        return
    if len(name) > PLAYLIST_NAME_MAX:
        await interaction.response.send_message(
            f"❗ Название должно быть не длиннее {PLAYLIST_NAME_MAX} символов.",
            ephemeral=True,
        )
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player or (not player.current and player.queue.is_empty):
        await interaction.response.send_message("❗ Очередь пуста.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    playlist_id = await db_create_playlist(interaction.user.id, name)
    if playlist_id is None:
        await interaction.followup.send(f"❗ Плейлист **{name}** уже существует.")
        return
    tracks = []
    if player.current:
        tracks.append(player.current)
    tracks.extend(list(player.queue)[:PLAYLIST_TRACK_LIMIT])
    for t in tracks:
        await db_add_track(playlist_id, t.title, t.uri or "", t.length)
    await interaction.followup.send(
        f"✅ Очередь сохранена как **{name}** — `{len(tracks)} треков`!"
    )


# ─────────────────────────────────────────────
#  Управление плеером
# ─────────────────────────────────────────────
@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not (player.playing or player.paused):
        await interaction.response.send_message("❗ Ничего не играет.")
        return

    if db_pool:
        settings = await db_get_settings(interaction.guild_id)
        if settings.get("vote_skip_enabled") and not await is_dj(interaction.user):
            await interaction.response.defer()
            await start_vote_skip(
                interaction.user, interaction.guild,
                interaction.channel, settings.get("vote_skip_percent", 50)
            )
            return

    if not await check_dj(interaction):
        return
    if player.queue.mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal
    await player.skip(force=True)
    if not interaction.response.is_done():
        await interaction.response.send_message("⏭ Пропущено.")


@tree.command(name="skipto", description="Перейти к треку по номеру в очереди")
@app_commands.describe(position="Номер трека")
async def skipto_cmd(interaction: discord.Interaction, position: int):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(f"❗ Укажи номер от 1 до {len(q)}.")
        return
    for _ in range(position - 1):
        try:
            q.get()
        except Exception:
            break
    if player.queue.mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal
    await player.skip(force=True)
    await interaction.response.send_message(f"⏩ Перехожу к треку #{position}.")


async def _do_seek(interaction: discord.Interaction, delta_seconds: int):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message("❗ Ничего не играет.")
        return
    if player.current.length <= 0:
        await interaction.response.send_message("❗ Нельзя перематывать прямой эфир.")
        return
    if abs(delta_seconds) > SEEK_MAX_SECONDS:
        await interaction.response.send_message(
            f"❗ Максимум {SEEK_MAX_SECONDS} секунд за раз."
        )
        return
    new_pos = max(0, player.position + delta_seconds * 1000)
    new_pos = min(new_pos, player.current.length)
    await player.seek(new_pos)
    arrow = "⏩" if delta_seconds > 0 else "⏪"
    await interaction.response.send_message(
        f"{arrow} Перемотано → `{format_duration(new_pos)}` / `{format_duration(player.current.length)}`"
    )


@tree.command(name="forward", description="Перемотать вперёд на N секунд")
@app_commands.describe(seconds="Сколько секунд пропустить вперёд")
async def forward_cmd(interaction: discord.Interaction, seconds: int):
    if seconds <= 0:
        await interaction.response.send_message("❗ Число секунд должно быть положительным.")
        return
    await _do_seek(interaction, seconds)


@tree.command(name="rewind", description="Перемотать назад на N секунд")
@app_commands.describe(seconds="Сколько секунд отмотать назад")
async def rewind_cmd(interaction: discord.Interaction, seconds: int):
    if seconds <= 0:
        await interaction.response.send_message("❗ Число секунд должно быть положительным.")
        return
    await _do_seek(interaction, -seconds)


@tree.command(name="pause", description="Пауза / продолжить")
async def pause_cmd(interaction: discord.Interaction):
    if not await check_dj(interaction):
        return
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
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        await full_disconnect(interaction.guild)
        await interaction.response.send_message("⏹ Остановлено.")
    else:
        await interaction.response.send_message("❗ Бот не в канале.")


@tree.command(name="volume", description="Громкость от 0 до 100")
@app_commands.describe(level="Уровень громкости (0–100)")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if not await check_dj(interaction):
        return
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
    if not await check_dj(interaction):
        return
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
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if player and len(player.queue) > 1:
        player.queue.shuffle()
        await interaction.response.send_message("🔀 Очередь перемешана.")
    else:
        await interaction.response.send_message("❗ Нечего перемешивать.")


@tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер трека в очереди")
async def remove_cmd(interaction: discord.Interaction, position: int):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(f"❗ Укажи номер от 1 до {len(q)}.")
        return
    all_tracks = list(q)
    removed = all_tracks.pop(position - 1)
    q.clear()
    for t in all_tracks:
        await q.put_wait(t)
    await interaction.response.send_message(f"🗑 Удалено: **{removed.title}**")


@tree.command(name="queue", description="Показать очередь с пагинацией")
async def queue_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or (not player.current and player.queue.is_empty):
        await interaction.response.send_message("📭 Очередь пуста.")
        return
    view = QueuePaginationView(interaction.guild, interaction.user.id)
    text = view.build_text()
    await interaction.response.send_message(text, view=view, ephemeral=True)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        pass


@tree.command(name="nowplaying", description="Что сейчас играет")
async def np_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message("📭 Ничего не играет.")
        return
    t = player.current
    link = f" — [открыть]({t.uri})" if t.uri else ""
    progress = make_progress_bar(player.position, t.length)
    await interaction.response.send_message(
        f"🎵 **{t.title}**{link}\n`{progress}`"
    )


@tree.command(name="lyrics", description="Показать текст текущей песни")
async def lyrics_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message("❗ Ничего не играет.")
        return
    await interaction.response.defer()
    title = player.current.title
    duration = player.current.length

    result = await fetch_lyrics(title, duration)
    if not result or not result.get("text"):
        sources = ["LRClib"]
        if GENIUS_TOKEN:
            sources.append("Genius")
        sources.append("lyrics.ovh")
        await interaction.followup.send(
            f"😕 Текст для **{title}** не найден.\n"
            f"_Проверено: {', '.join(sources)}._\n"
            f"_Если трек называется нестандартно, попробуй формат 'Артист - Название'_"
        )
        return

    text = result["text"]
    if len(text) > LYRICS_MAX_CHARS:
        text = text[:LYRICS_MAX_CHARS] + "\n_...продолжение текста обрезано_"

    synced_mark = " 🎤" if result.get("synced") else ""
    await interaction.followup.send(
        f"📝 **{title}** _(via {result['source']}{synced_mark})_\n\n{text}"
    )


@tree.command(name="history", description="История последних треков")
async def history_cmd(interaction: discord.Interaction):
    history = track_history.get(interaction.guild_id, [])
    if not history:
        await interaction.response.send_message("📭 История пуста.", ephemeral=True)
        return
    lines = ["**История треков:**\n"]
    for i, t in enumerate(history, 1):
        link = f" — [открыть]({t['uri']})" if t.get("uri") else ""
        lines.append(f"`{i}.` {t['title']} `[{format_duration(t['length'])}]`{link}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="stats", description="Статистика прослушивания на сервере")
async def stats_cmd(interaction: discord.Interaction):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    stats = await db_get_stats(interaction.guild_id)
    if not stats:
        await interaction.response.send_message(
            "📭 Статистика пуста — включи первый трек!", ephemeral=True
        )
        return
    total_hours = stats["total_ms"] // 3600000
    total_minutes = (stats["total_ms"] % 3600000) // 60000
    embed = discord.Embed(title=f"📊 Статистика {BOT_NAME}", color=discord.Color.blurple())
    embed.add_field(name="🎵 Треков сыграно", value=f"**{stats['tracks_played']}**", inline=True)
    embed.add_field(name="⏱ Общее время",
                    value=f"**{total_hours}ч {total_minutes}м**", inline=True)
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
#  Эффекты
# ─────────────────────────────────────────────
EFFECT_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in EFFECTS.items()]


@tree.command(name="effect", description="Применить звуковой эффект (один за раз)")
@app_commands.describe(effect="Выбери эффект")
@app_commands.choices(effect=EFFECT_CHOICES)
async def effect_cmd(interaction: discord.Interaction, effect: app_commands.Choice[str]):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    await interaction.response.defer()
    try:
        await apply_effect(player, effect.value)
        current_effect[interaction.guild_id] = effect.value
        if effect.value == "off":
            await interaction.followup.send("✅ Эффекты отключены.")
        else:
            await interaction.followup.send(f"✨ Эффект: **{effect.name}**")
    except Exception as e:
        log.warning("Effect error: %s", e)
        await interaction.followup.send(f"❗ Ошибка: {e}")


# ─────────────────────────────────────────────
#  Настройки сервера
# ─────────────────────────────────────────────
settings_group = app_commands.Group(
    name="settings",
    description="Настройки бота для сервера (только администраторы)",
    default_permissions=discord.Permissions(manage_guild=True)
)


@settings_group.command(name="show", description="Показать текущие настройки")
async def settings_show(interaction: discord.Interaction):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    settings = await db_get_settings(interaction.guild_id)
    dj_role = interaction.guild.get_role(settings["dj_role_id"]) if settings.get("dj_role_id") else None
    embed = discord.Embed(title=f"⚙️ Настройки {BOT_NAME}", color=discord.Color.blurple())
    embed.add_field(
        name="🎧 DJ-роль",
        value=dj_role.mention if dj_role else "_не установлена (все могут управлять)_",
        inline=False
    )
    embed.add_field(
        name="⏭ Голосование за скип",
        value=f"{'✅ Включено' if settings['vote_skip_enabled'] else '❌ Выключено'} | "
              f"Порог: **{settings['vote_skip_percent']}%**",
        inline=False
    )
    embed.add_field(
        name="🎵 Лимит треков на пользователя",
        value=f"**{settings['track_limit']}** {'треков' if settings['track_limit'] > 0 else '(без лимита)'}",
        inline=False
    )
    embed.add_field(
        name="⚖️ Справедливая очередь",
        value=f"{'✅ Включена' if settings.get('fair_queue') else '❌ Выключена'}",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@settings_group.command(name="djrole", description="Установить DJ-роль")
@app_commands.describe(role="Роль которая может управлять ботом")
async def settings_djrole(interaction: discord.Interaction, role: discord.Role):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    await db_save_settings(interaction.guild_id, dj_role_id=role.id)
    await interaction.response.send_message(
        f"✅ DJ-роль установлена: {role.mention}\n"
        f"Теперь только пользователи с этой ролью (и администраторы) могут управлять ботом.",
        ephemeral=True
    )


@settings_group.command(name="djrole_remove", description="Убрать DJ-роль (все смогут управлять)")
async def settings_djrole_remove(interaction: discord.Interaction):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    await db_save_settings(interaction.guild_id, dj_role_id=None)
    await interaction.response.send_message(
        "✅ DJ-роль убрана — теперь все могут управлять ботом.", ephemeral=True
    )


@settings_group.command(name="voteskip", description="Включить/выключить голосование за скип")
@app_commands.describe(enabled="Включить голосование", percent="Процент голосов для скипа (1-100)")
async def settings_voteskip(interaction: discord.Interaction,
                             enabled: bool, percent: int = 50):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    if not 1 <= percent <= 100:
        await interaction.response.send_message("❗ Процент должен быть от 1 до 100.", ephemeral=True)
        return
    await db_save_settings(interaction.guild_id,
                           vote_skip_enabled=enabled,
                           vote_skip_percent=percent)
    status = "включено ✅" if enabled else "выключено ❌"
    await interaction.response.send_message(
        f"✅ Голосование за скип {status}\n"
        f"Порог: **{percent}%** голосов слушателей.",
        ephemeral=True
    )


@settings_group.command(name="tracklimit",
                         description="Установить лимит треков на пользователя (0 = без лимита)")
@app_commands.describe(limit="Максимум треков за сессию (0 = без лимита)")
async def settings_tracklimit(interaction: discord.Interaction, limit: int):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    if limit < 0:
        await interaction.response.send_message("❗ Лимит не может быть отрицательным.", ephemeral=True)
        return
    await db_save_settings(interaction.guild_id, track_limit=limit)
    if limit == 0:
        await interaction.response.send_message("✅ Лимит треков снят.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"✅ Лимит установлен: **{limit} треков** на пользователя за сессию.\n"
            f"_DJ-роль снимает лимит._",
            ephemeral=True
        )


@settings_group.command(name="fairqueue", description="Справедливая очередь (чередовать треки пользователей)")
@app_commands.describe(enabled="Включить чередование")
async def settings_fairqueue(interaction: discord.Interaction, enabled: bool):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    await db_save_settings(interaction.guild_id, fair_queue=enabled)
    if enabled:
        await interaction.response.send_message(
            "⚖️ **Справедливая очередь включена.**\n"
            "Когда кто-то добавляет плейлист, его треки чередуются "
            "с треками других пользователей (через один).\n"
            "_Одиночные треки добавляются как обычно._",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "❌ Справедливая очередь выключена. Треки ставятся подряд.",
            ephemeral=True,
        )


tree.add_command(settings_group)


# ─────────────────────────────────────────────
#  Плейлисты
# ─────────────────────────────────────────────
playlist_group = app_commands.Group(name="playlist", description="Личные плейлисты")


def _validate_playlist_name(name: str) -> Optional[str]:
    if not name:
        return None
    cleaned = name.strip()
    if not cleaned or len(cleaned) > PLAYLIST_NAME_MAX:
        return None
    return cleaned


@playlist_group.command(name="create", description="Создать плейлист")
@app_commands.describe(name="Название")
async def pl_create(interaction: discord.Interaction, name: str):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.response.send_message(
            f"❗ Название должно быть не пустым и не длиннее {PLAYLIST_NAME_MAX} символов.",
            ephemeral=True,
        )
        return
    playlist_id = await db_create_playlist(interaction.user.id, clean)
    if playlist_id is None:
        await interaction.response.send_message(
            f"❗ Плейлист **{clean}** уже существует.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"✅ Плейлист **{clean}** создан!", ephemeral=True)


@playlist_group.command(name="list", description="Твои плейлисты")
async def pl_list(interaction: discord.Interaction):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    playlists = await db_get_user_playlists(interaction.user.id)
    if not playlists:
        await interaction.response.send_message(
            "📭 Нет плейлистов. Создай через `/playlist create`", ephemeral=True
        )
        return
    lines = ["**Твои плейлисты:**\n"]
    for i, p in enumerate(playlists, 1):
        lines.append(f"`{i}.` **{p['name']}** — {p['track_count']} треков")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@playlist_group.command(name="play", description="Воспроизвести плейлист")
@app_commands.describe(name="Название")
async def pl_play(interaction: discord.Interaction, name: str):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    await interaction.response.defer()
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.followup.send("❗ Неверное название.")
        return
    if not interaction.user.voice:
        await interaction.followup.send("❗ Зайди в голосовой канал сначала.")
        return
    playlist = await db_get_playlist(interaction.user.id, clean)
    if not playlist:
        await interaction.followup.send(f"❗ Плейлист **{clean}** не найден.")
        return
    tracks = await db_get_tracks(playlist["id"])
    if not tracks:
        await interaction.followup.send(f"❗ Плейлист **{clean}** пуст.")
        return
    if not await check_track_limit(interaction, min(len(tracks), PLAYLIST_TRACK_LIMIT)):
        return

    player = await ensure_voice_connection(interaction)
    if player is None:
        await interaction.followup.send("❗ Не удалось подключиться к голосовому каналу.")
        return

    msg = await interaction.followup.send(f"⏳ Загружаю **{clean}**...", wait=True)
    loaded = []
    for t in tracks[:PLAYLIST_TRACK_LIMIT]:
        try:
            results = await wavelink.Playable.search(t["uri"])
            if results:
                track = results[0] if isinstance(results, list) else results.tracks[0]
                loaded.append(track)
        except Exception as e:
            log.debug("Playlist track load error: %s", e)
            continue

    if not loaded:
        await msg.edit(content=f"😕 Не удалось загрузить треки из **{clean}**.")
        return

    fair = await get_fair_queue_enabled(interaction.guild_id)
    if not player.playing:
        first = loaded[0]
        rest = loaded[1:]
        tag_track(interaction.guild_id, first, interaction.user.id)
        for t in rest:
            tag_track(interaction.guild_id, t, interaction.user.id)
        for t in rest:
            await player.queue.put_wait(t)
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        await player.play(first)
    else:
        await add_tracks_fairly(player, loaded, interaction.user.id, enabled=fair)
        await msg.edit(content=f"📋 **{clean}** добавлен — `{len(loaded)} треков`")
    increment_user_track_count(interaction.guild_id, interaction.user.id, len(loaded))


@playlist_group.command(name="delete", description="Удалить плейлист")
@app_commands.describe(name="Название")
async def pl_delete(interaction: discord.Interaction, name: str):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.response.send_message("❗ Неверное название.", ephemeral=True)
        return
    deleted = await db_delete_playlist(interaction.user.id, clean)
    if not deleted:
        await interaction.response.send_message(
            f"❗ Плейлист **{clean}** не найден.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"🗑 Плейлист **{clean}** удалён.", ephemeral=True)


@playlist_group.command(name="addtrack", description="Добавить текущий трек в плейлист")
@app_commands.describe(name="Название")
async def pl_addtrack(interaction: discord.Interaction, name: str):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.response.send_message("❗ Неверное название.", ephemeral=True)
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message("❗ Ничего не играет.", ephemeral=True)
        return
    playlist = await db_get_playlist(interaction.user.id, clean)
    if not playlist:
        await interaction.response.send_message(
            f"❗ Плейлист **{clean}** не найден.", ephemeral=True
        )
        return
    track = player.current
    await db_add_track(playlist["id"], track.title, track.uri or "", track.length)
    await interaction.response.send_message(
        f"✅ **{track.title}** добавлен в **{clean}**!", ephemeral=True
    )


@playlist_group.command(name="tracks", description="Треки плейлиста")
@app_commands.describe(name="Название")
async def pl_tracks(interaction: discord.Interaction, name: str):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.response.send_message("❗ Неверное название.", ephemeral=True)
        return
    playlist = await db_get_playlist(interaction.user.id, clean)
    if not playlist:
        await interaction.response.send_message(
            f"❗ Плейлист **{clean}** не найден.", ephemeral=True
        )
        return
    tracks = await db_get_tracks(playlist["id"])
    if not tracks:
        await interaction.response.send_message(
            f"📭 Плейлист **{clean}** пуст.", ephemeral=True
        )
        return
    lines = [f"**{clean}** — {len(tracks)} треков\n"]
    for t in tracks[:PLAYLIST_TRACKS_SHOW]:
        lines.append(f"`{t['position']}.` {t['title']} `[{format_duration(t['duration'])}]`")
    if len(tracks) > PLAYLIST_TRACKS_SHOW:
        lines.append(f"_...и ещё {len(tracks) - PLAYLIST_TRACKS_SHOW} треков_")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


tree.add_command(playlist_group)


# ─────────────────────────────────────────────
#  День рождения
# ─────────────────────────────────────────────
birthday_group = app_commands.Group(name="birthday", description="День рождения")


@birthday_group.command(name="set", description="Установить свой день рождения")
@app_commands.describe(day="День (1-31)", month="Месяц (1-12)")
async def birthday_set(interaction: discord.Interaction, day: int, month: int):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    if not (1 <= day <= 31) or not (1 <= month <= 12):
        await interaction.response.send_message("❗ Неверная дата.", ephemeral=True)
        return
    try:
        datetime.date(2024, month, day)
    except ValueError:
        await interaction.response.send_message("❗ Такой даты не существует.", ephemeral=True)
        return
    await db_set_birthday(interaction.user.id, day, month)
    month_names = ["января","февраля","марта","апреля","мая","июня",
                   "июля","августа","сентября","октября","ноября","декабря"]
    await interaction.response.send_message(
        f"🎂 ДР сохранён: **{day} {month_names[month-1]}** 🎉", ephemeral=True
    )


@birthday_group.command(name="song", description="Изменить поздравительную песню")
@app_commands.describe(query="Название трека")
async def birthday_song(interaction: discord.Interaction, query: str):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    query = (query or "").strip()
    if not query or len(query) > 200:
        await interaction.response.send_message("❗ Название должно быть 1-200 символов.", ephemeral=True)
        return
    birthday = await db_get_birthday(interaction.user.id)
    if not birthday:
        await interaction.response.send_message(
            "❗ Сначала установи ДР через `/birthday set`", ephemeral=True
        )
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE birthdays SET birthday_song=$1 WHERE user_id=$2",
            query, interaction.user.id
        )
    await interaction.response.send_message(
        f"🎵 Поздравительная песня: **{query}**", ephemeral=True
    )


@birthday_group.command(name="remove", description="Удалить свой день рождения")
async def birthday_remove(interaction: discord.Interaction):
    if not db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM birthdays WHERE user_id=$1", interaction.user.id)
    await interaction.response.send_message("✅ День рождения удалён.", ephemeral=True)


tree.add_command(birthday_group)


# ─────────────────────────────────────────────
#  Help
# ─────────────────────────────────────────────
@tree.command(name="help", description="Список всех команд")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title=f"🎵 {BOT_NAME} — Команды", color=discord.Color.blurple())
    embed.add_field(name="▶️ Воспроизведение", value=
        "`/play <запрос>` — YouTube, SoundCloud, Spotify, Я.Музыка (автоопределение)\n"
        "`/skip` — пропустить (или начать голосование)\n"
        "`/skipto <номер>` — перейти к треку\n"
        "`/forward <сек>` — перемотать вперёд\n"
        "`/rewind <сек>` — перемотать назад\n"
        "`/pause` — пауза / продолжить\n"
        "`/stop` — остановить и выйти",
        inline=False
    )
    embed.add_field(name="🎛️ Настройки", value=
        "`/volume <0-100>` — громкость\n"
        "`/loop` — режим повтора\n"
        "`/shuffle` — перемешать\n"
        "`/effect <эффект>` — звуковой эффект\n"
        "`/lyrics` — текст текущей песни",
        inline=False
    )
    embed.add_field(name="📋 Очередь", value=
        "`/queue` — показать очередь с пагинацией\n"
        "`/nowplaying` — текущий трек\n"
        "`/remove <номер>` — убрать трек\n"
        "`/history` — история\n"
        "`/stats` — статистика\n"
        "`/savequeue <название>` — сохранить очередь",
        inline=False
    )
    embed.add_field(name="💾 Плейлисты", value=
        "`/playlist create/list/play/delete/tracks/addtrack`",
        inline=False
    )
    embed.add_field(name="⚙️ Настройки сервера (админ)", value=
        "`/settings show` — текущие настройки\n"
        "`/settings djrole <роль>` — DJ-роль\n"
        "`/settings djrole_remove` — убрать DJ-роль\n"
        "`/settings voteskip <вкл/выкл> [%]` — голосование\n"
        "`/settings tracklimit <число>` — лимит треков\n"
        "`/settings fairqueue <вкл/выкл>` — справедливая очередь",
        inline=False
    )
    embed.add_field(name="🎂 День рождения", value=
        "`/birthday set/song/remove` — бот поздравит именинника при заходе в голосовой канал",
        inline=False
    )
    embed.add_field(name="🎮 Кнопки под «Сейчас играет»", value=
        "⏸ пауза  ⏭ скип  🔁 повтор  🔀 shuffle  📋 очередь\n"
        "🔉 −10%  🔊 +10%  💾 в плейлист  ⏹ стоп",
        inline=False
    )
    embed.set_footer(text=f"{BOT_NAME} • Эффекты: bassboost, nightcore, vaporwave, slowmo, 8d")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.error
async def on_app_command_error(interaction: discord.Interaction,
                                error: app_commands.AppCommandError):
    log.error("Slash command error: %s", error, exc_info=True)
    err_msg = "❗ Произошла ошибка. Попробуй ещё раз."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(err_msg, ephemeral=True)
        else:
            await interaction.response.send_message(err_msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ─────────────────────────────────────────────
#  Фоновые задачи
# ─────────────────────────────────────────────
async def reset_birthday_flags_daily():
    while True:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        moscow = now_utc + datetime.timedelta(hours=3)
        next_run = moscow.replace(hour=0, minute=5, second=0, microsecond=0)
        if next_run <= moscow:
            next_run += datetime.timedelta(days=1)
        wait_seconds = (next_run - moscow).total_seconds()
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            return
        for state in player_state.values():
            state.pop("birthday_greeted", None)
        log.info("Birthday greeted flags cleared")


# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    global _ready_once
    log.info("%s запущен как %s", BOT_NAME, bot.user)
    if _ready_once:
        log.info("on_ready сработал повторно — пропускаю инициализацию.")
        return
    _ready_once = True

    if DATABASE_URL:
        try:
            await init_db()
            log.info("База данных подключена")
        except Exception as e:
            log.error("Ошибка инициализации БД: %s", e)
    else:
        log.warning("DATABASE_URL не задан — БД отключена")

    nodes = [wavelink.Node(**n) for n in NODES]
    try:
        await wavelink.Pool.connect(nodes=nodes, client=bot)
        log.info("Подключение к Lavalink инициировано")
    except Exception as e:
        log.error("Lavalink connect error: %s", e)

    try:
        synced = await tree.sync()
        log.info("Синхронизировано %d команд", len(synced))
    except Exception as e:
        log.error("tree.sync error: %s", e)

    bot.loop.create_task(reset_birthday_flags_daily())

    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="/play"
    ))


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN не задан в .env")
    bot.run(DISCORD_TOKEN, log_handler=None)