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
#  Spotify (fallback)
# ─────────────────────────────────────────────
_spotify_token: Optional[str] = None
_spotify_token_expires: float = 0.0

async def get_spotify_token() -> Optional[str]:
    global _spotify_token, _spotify_token_expires
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    if _spotify_token and time.time() < _spotify_token_expires:
        return _spotify_token
    creds = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()
    if SPOTIFY_REFRESH_TOKEN:
        body = {"grant_type": "refresh_token", "refresh_token": SPOTIFY_REFRESH_TOKEN}
    else:
        body = {"grant_type": "client_credentials"}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data=body,
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
    # Формат: spotify.com/[intl-XX/]track|album|playlist/ID[?si=...]
    m = re.search(
        r'spotify\.com(?:/intl-[a-z]+)?/(track|album|playlist)/([A-Za-z0-9]+)',
        url
    )
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'spotify:(track|album|playlist):([A-Za-z0-9]+)', url)
    if m:
        return m.group(1), m.group(2)
    return None


async def fetch_spotify_tracks(url: str) -> tuple[Optional[list], Optional[str]]:
    """
    Возвращает (tracks, error_reason).
    Если успех — (список треков, None).
    Если ошибка — (None, описание причины).
    """
    parsed = parse_spotify_url(url)
    if not parsed:
        return None, "Не удалось распознать ссылку Spotify"
    sp_type, sp_id = parsed
    token = await get_spotify_token()
    if not token:
        return None, "Spotify не настроен или токен недоступен"
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
                    if r.status == 404:
                        return None, "track_not_found"
                    if r.status == 401:
                        return None, "spotify_token_invalid"
                    if r.status != 200:
                        return None, f"spotify_http_{r.status}"
                    d = await r.json()
                    if d.get("artists") and d.get("name"):
                        tracks.append({"title": d["name"], "artist": d["artists"][0]["name"]})
            elif sp_type == "album":
                # Пагинация для альбома (макс 50 за запрос)
                offset = 0
                page_size = 50
                while len(tracks) < SPOTIFY_TRACK_LIMIT:
                    async with s.get(
                        f"https://api.spotify.com/v1/albums/{sp_id}/tracks"
                        f"?limit={page_size}&offset={offset}",
                        headers=headers,
                    ) as r:
                        if r.status == 404:
                            return None, "album_not_found"
                        if r.status == 401:
                            return None, "spotify_token_invalid"
                        if r.status != 200:
                            return None, f"spotify_http_{r.status}"
                        d = await r.json()
                        items = d.get("items", [])
                        if not items:
                            break
                        for item in items:
                            if item and item.get("name") and item.get("artists"):
                                tracks.append({
                                    "title": item["name"],
                                    "artist": item["artists"][0]["name"],
                                })
                                if len(tracks) >= SPOTIFY_TRACK_LIMIT:
                                    break
                        if len(items) < page_size:
                            break
                        offset += page_size
                log.info("Spotify API album: загружено %d треков", len(tracks))
            elif sp_type == "playlist":
                # Пагинация: Spotify API отдаёт максимум 100 треков за запрос.
                # Берём страницами по 100 пока не наберём SPOTIFY_TRACK_LIMIT.
                offset = 0
                page_size = 100
                while len(tracks) < SPOTIFY_TRACK_LIMIT:
                    async with s.get(
                        f"https://api.spotify.com/v1/playlists/{sp_id}/tracks"
                        f"?limit={page_size}&offset={offset}",
                        headers=headers,
                    ) as r:
                        if r.status == 404:
                            return None, "playlist_not_found"
                        if r.status == 401:
                            return None, "spotify_token_invalid"
                        if r.status != 200:
                            return None, f"spotify_http_{r.status}"
                        d = await r.json()
                        items = d.get("items", [])
                        if not items:
                            break
                        for item in items:
                            t = item.get("track") if item else None
                            if t and t.get("name") and t.get("artists"):
                                tracks.append({
                                    "title": t["name"],
                                    "artist": t["artists"][0]["name"],
                                })
                                if len(tracks) >= SPOTIFY_TRACK_LIMIT:
                                    break
                        # Если страница неполная — больше нечего грузить
                        if len(items) < page_size:
                            break
                        offset += page_size
                log.info("Spotify API playlist: загружено %d треков", len(tracks))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Spotify fetch error: %s", e)
        return None, "spotify_network_error"

    if not tracks:
        return None, "spotify_empty"
    return tracks, None


async def fetch_spotify_via_embed(sp_id: str) -> Optional[list]:
    """
    Парсит публичную embed-страницу Spotify-плейлиста.
    Используется как fallback когда API даёт 403/404 (закрытое Development Mode,
    плейлист не в allowlist, или Spotify-owned плейлист).

    Embed-страница (open.spotify.com/embed/playlist/<ID>) содержит JSON
    со всеми треками публичного плейлиста, и для неё не требуется API-токен.

    Минусы:
    - Только публичные плейлисты
    - Spotify может поменять формат страницы (тогда регэксп сломается)
    - Лимит ~100 треков (что лежит в первой "странице")
    """
    url = f"https://open.spotify.com/embed/playlist/{sp_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers=headers,
        ) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    log.warning("Spotify embed HTTP %s", r.status)
                    return None
                html = await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Spotify embed fetch error: %s", e)
        return None

    # Spotify embed-страница содержит JSON в скрипте
    # <script id="__NEXT_DATA__" type="application/json">{...}</script>
    import json as _json
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        log.warning("Spotify embed: __NEXT_DATA__ не найден в HTML")
        return None

    try:
        data = _json.loads(m.group(1))
    except _json.JSONDecodeError as e:
        log.warning("Spotify embed: JSON parse error: %s", e)
        return None

    # Структура: props.pageProps.state.data.entity.trackList[].title/subtitle
    # subtitle обычно "Артист 1, Артист 2"
    try:
        track_list = (
            data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity", {})
                .get("trackList", [])
        )
    except (AttributeError, TypeError):
        track_list = []

    if not track_list:
        log.warning("Spotify embed: trackList пустой или не найден")
        return None

    tracks = []
    for t in track_list[:SPOTIFY_TRACK_LIMIT]:
        title = t.get("title")
        # subtitle — это исполнитель(ли) через запятую
        artist = t.get("subtitle") or ""
        if title:
            # Если есть несколько артистов через запятую — берём первого для лучшего поиска
            first_artist = artist.split(",")[0].strip() if artist else ""
            tracks.append({
                "title": title,
                "artist": first_artist,
            })

    log.info("Spotify embed: распарсили %d треков", len(tracks))
    return tracks if tracks else None


async def fetch_spotify_via_embed_track(sp_id: str) -> Optional[list]:
    """То же что и для плейлиста, но для одиночного трека."""
    url = f"https://open.spotify.com/embed/track/{sp_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers=headers,
        ) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                html = await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    import json as _json
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None

    try:
        data = _json.loads(m.group(1))
    except _json.JSONDecodeError:
        return None

    try:
        entity = (
            data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity", {})
        )
        title = entity.get("title") or entity.get("name")
        # У трека artist может быть в "artists" массиве или в subtitle
        artists = entity.get("artists", [])
        if artists and isinstance(artists, list):
            artist = artists[0].get("name", "")
        else:
            artist = entity.get("subtitle", "").split(",")[0].strip()
        if title:
            return [{"title": title, "artist": artist}]
    except (AttributeError, TypeError):
        pass
    return None


async def fetch_spotify_via_embed_album(sp_id: str) -> Optional[list]:
    """То же для альбома."""
    url = f"https://open.spotify.com/embed/album/{sp_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers=headers,
        ) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                html = await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    import json as _json
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None

    try:
        data = _json.loads(m.group(1))
    except _json.JSONDecodeError:
        return None

    try:
        entity = (
            data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity", {})
        )
        track_list = entity.get("trackList", [])
        # Для альбома исполнитель часто общий, в entity.artists
        artists = entity.get("artists", [])
        if artists and isinstance(artists, list):
            common_artist = artists[0].get("name", "")
        else:
            common_artist = ""
    except (AttributeError, TypeError):
        track_list = []
        common_artist = ""

    if not track_list:
        return None

    tracks = []
    for t in track_list[:SPOTIFY_TRACK_LIMIT]:
        title = t.get("title")
        artist = t.get("subtitle") or common_artist or ""
        first_artist = artist.split(",")[0].strip() if artist else ""
        if title:
            tracks.append({"title": title, "artist": first_artist})
    return tracks if tracks else None


async def fetch_spotify_with_fallback(url: str) -> tuple[Optional[list], Optional[str]]:
    """
    Полная цепочка получения треков из Spotify:
    1. Сначала пробует API (быстро, надёжно для разрешённых плейлистов)
    2. Если API дал 403/404 (Development Mode ограничения, Spotify-owned плейлисты)
       — переходит на парсинг embed-страницы
    """
    # Попытка 1: официальный API
    tracks, error = await fetch_spotify_tracks(url)
    if tracks:
        return tracks, None

    # API не справился. Если это разрешимая ошибка — попробуем embed
    fallback_errors = (
        "playlist_not_found", "track_not_found", "album_not_found",
        "spotify_http_403", "spotify_http_404",
    )
    if error in fallback_errors:
        log.info("Spotify API не справился (%s), пробую embed-парсинг", error)
        parsed = parse_spotify_url(url)
        if not parsed:
            return None, error
        sp_type, sp_id = parsed
        embed_tracks = None
        if sp_type == "playlist":
            embed_tracks = await fetch_spotify_via_embed(sp_id)
        elif sp_type == "track":
            embed_tracks = await fetch_spotify_via_embed_track(sp_id)
        elif sp_type == "album":
            embed_tracks = await fetch_spotify_via_embed_album(sp_id)

        if embed_tracks:
            log.info("Spotify embed-парсинг успешен: %d треков", len(embed_tracks))
            return embed_tracks, None
        log.info("Spotify embed-парсинг не нашёл треков")
        # Возвращаем оригинальную ошибку API
        return None, error

    # Ошибка не из тех, что лечится embed (например, токен невалидный)
    return None, error

