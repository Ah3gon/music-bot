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
import core
from core import *

from helpers import cancel_empty_channel_timer, cancel_idle_timer

# ─────────────────────────────────────────────
#  Определение источника
# ─────────────────────────────────────────────
def detect_source_from_url(query: str) -> wavelink.TrackSource:
    q = query.lower()
    if "soundcloud.com" in q or q.startswith("sc:"):
        return wavelink.TrackSource.SoundCloud
    return wavelink.TrackSource.YouTube


async def safe_play_track(
    player: wavelink.Player,
    track,
    retries: int = 3,
) -> bool:
    """
    Безопасно запускает трек с повтором при ошибках.
    Возвращает True при успехе, False если все попытки провалились.
    """
    last_error = None
    for attempt in range(retries):
        try:
            await player.play(track)
            return True
        except wavelink.LavalinkException as e:
            err_str = str(e).lower()
            last_error = e
            # 404 — нода забыла про player'а. Wavelink сам пересоздаст
            # при следующем play, надо подождать и повторить
            if "status=404" in err_str or "not found" in err_str:
                log.warning("Lavalink 404 при play (попытка %d/%d), жду 1 сек",
                            attempt + 1, retries)
                await asyncio.sleep(1)
                continue
            # Другая Lavalink-ошибка — тоже пробуем ещё
            log.warning("LavalinkException при play (попытка %d/%d): %s",
                        attempt + 1, retries, e)
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        except Exception as e:
            last_error = e
            log.warning("Play error (попытка %d/%d): %s", attempt + 1, retries, e)
            await asyncio.sleep(0.5 * (attempt + 1))
            continue

    log.error("Не удалось запустить трек после %d попыток: %s", retries, last_error)
    return False


def get_healthy_node() -> Optional[wavelink.Node]:
    """Возвращает первую CONNECTED-ноду или None если все упали."""
    for node in wavelink.Pool.nodes.values():
        if node.status == wavelink.NodeStatus.CONNECTED:
            return node
    return None


async def connect_to_voice(
    voice_channel: discord.VoiceChannel,
    timeout: float = 20.0,
) -> Optional[wavelink.Player]:
    """
    Подключается к голосовому каналу с явным указанием рабочей ноды.
    Если Wavelink сам выбирает ноду — он может зацепиться за упавшую,
    и подключение зависнет на 30 сек таймаута.

    Возвращает Player или None если не удалось.
    """
    node = get_healthy_node()
    if node is None:
        log.warning("connect_to_voice: нет ни одной живой Lavalink-ноды")
        return None
    try:
        player = await asyncio.wait_for(
            voice_channel.connect(cls=wavelink.Player, self_deaf=True),
            timeout=timeout,
        )
        return player
    except asyncio.TimeoutError:
        log.warning("connect_to_voice: таймаут (%.1fs) при подключении к %s",
                    timeout, voice_channel.name)
        return None
    except Exception as e:
        log.warning("connect_to_voice: ошибка %s при подключении к %s",
                    e, voice_channel.name)
        return None


async def search_with_node_fallback(
    query: str,
    source: wavelink.TrackSource,
    max_nodes: int = 3,
    timeout: float = 15.0,
):
    """
    Ищет трек, перебирая разные Lavalink-ноды если первая дала ошибку.

    Wavelink сам выбирает ноду через Pool, но не переключается при ошибках
    типа "Failed to Load Tracks" (когда YouTube банит конкретную ноду).
    Эта функция перебирает доступные ноды вручную.

    timeout: на каждую ноду максимум столько секунд. Если нода зависла
    (например, внутри LavaSrc на 404 от Spotify) — обрываем и идём к следующей.

    Возвращает (results, last_error).
    """
    nodes = list(wavelink.Pool.nodes.values())
    if not nodes:
        return None, Exception("Нет доступных Lavalink-нод")

    nodes = sorted(
        nodes,
        key=lambda n: 0 if n.status == wavelink.NodeStatus.CONNECTED else 1
    )[:max_nodes]

    last_error = None
    for i, node in enumerate(nodes):
        try:
            results = await asyncio.wait_for(
                wavelink.Playable.search(query, source=source, node=node),
                timeout=timeout,
            )
            if results:
                if i > 0:
                    log.info("Поиск удался на ноде #%d (%s)", i + 1, node.uri)
                return results, None
            last_error = None
        except asyncio.TimeoutError:
            log.warning("Search timeout (%.1fs) на ноде %s", timeout, node.uri)
            last_error = Exception(f"Таймаут поиска на {node.uri}")
            continue
        except Exception as e:
            log.warning("Search error на ноде %s: %s", node.uri, e)
            last_error = e
            continue

    return None, last_error


def parse_youtube_playlist_id(url: str) -> Optional[str]:
    """Извлекает ID плейлиста YouTube из ссылки."""
    m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None


async def fetch_youtube_playlist_video_ids(playlist_id: str, limit: int = 200) -> Optional[list]:
    """
    Получает video_id из YouTube-плейлиста через парсинг публичной страницы.
    Без YouTube API ключа.

    Возвращает список video_id или None при ошибке.
    """
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        ) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    log.warning("YT playlist HTTP %s", r.status)
                    return None
                html = await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("YT playlist fetch error: %s", e)
        return None

    # Видео-ID на странице плейлиста встречаются в нескольких местах.
    # Самый надёжный способ — найти все уникальные videoId в JSON-данных страницы.
    video_ids = []
    seen = set()
    for match in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
        vid = match.group(1)
        if vid not in seen:
            seen.add(vid)
            video_ids.append(vid)
            if len(video_ids) >= limit:
                break

    if not video_ids:
        log.warning("YT playlist: не нашли ни одного videoId")
        return None

    log.info("YT playlist: нашли %d видео", len(video_ids))
    return video_ids


async def ensure_voice_connection(
    interaction: discord.Interaction,
) -> Optional[wavelink.Player]:
    if not interaction.user.voice or not interaction.user.voice.channel:
        return None
    player: wavelink.Player = interaction.guild.voice_client
    try:
        if player is None:
            player = await connect_to_voice(interaction.user.voice.channel)
            if player is None:
                return None
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

