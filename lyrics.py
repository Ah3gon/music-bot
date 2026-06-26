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

