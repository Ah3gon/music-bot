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
#  Конфигурация
# ─────────────────────────────────────────────
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
DATABASE_URL          = os.getenv("DATABASE_URL")
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")
GENIUS_TOKEN          = os.getenv("GENIUS_TOKEN", "")
IDLE_TIMEOUT          = 300
EMPTY_CH_TIMEOUT      = 60
SPOTIFY_TRACK_LIMIT   = 500
PLAYLIST_TRACK_LIMIT  = 500
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
    {"uri": os.getenv("LAVALINK_URI", "http://127.0.0.1:2333"),
     "password": os.getenv("LAVALINK_PASSWORD", "")},
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


# ─────────────────────────────────────────────
#  Хелперы состояния плеера
# ─────────────────────────────────────────────
def get_player_state(guild_id: int) -> dict:
    return player_state.setdefault(guild_id, {})


def clear_player_state(guild_id: int):
    player_state.pop(guild_id, None)



BRAND_COLOR = 0x8B5CF6  # фирменный фиолетовый (под логотип)

__all__ = ['BRAND_COLOR', 'BOT_NAME', 'DATABASE_URL', 'DISCORD_TOKEN', 'EFFECTS', 'EMPTY_CH_TIMEOUT', 'GENIUS_TOKEN', 'HISTORY_LIMIT', 'IDLE_TIMEOUT', 'LYRICS_MAX_CHARS', 'MAX_INT32', 'NODES', 'PLAYLIST_NAME_MAX', 'PLAYLIST_TRACKS_SHOW', 'PLAYLIST_TRACK_LIMIT', 'QUEUE_DEFAULT_SHOW', 'SEEK_MAX_SECONDS', 'SPOTIFY_CLIENT_ID', 'SPOTIFY_CLIENT_SECRET', 'SPOTIFY_REFRESH_TOKEN', 'SPOTIFY_TRACK_LIMIT', 'TRACKS_PER_PAGE', 'active_votes', 'bot', 'clear_player_state', 'current_effect', 'empty_channel_tasks', 'get_player_state', 'idle_tasks', 'intents', 'log', 'player_state', 'track_history', 'track_user_map', 'tree', 'user_track_counts']
