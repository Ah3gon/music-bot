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

from database import db_add_track, db_create_playlist
from helpers import add_tracks_fairly, check_track_limit, format_duration, get_fair_queue_enabled, increment_user_track_count, tag_track
from playback import detect_source_from_url, ensure_voice_connection, fetch_youtube_playlist_video_ids, parse_youtube_playlist_id, safe_play_track, search_with_node_fallback
from spotify import fetch_spotify_with_fallback, parse_spotify_url
from views import TrackSelectView

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
    search_error = None
    is_spotify_url = "spotify.com" in query.lower() or query.lower().startswith("spotify:")

    # Для Spotify-ссылок сразу идём в Spotify-обработчик, минуя Lavalink.
    # Lavalink+LavaSrc на Spotify-плейлистах часто зависает (особенно на
    # тех, что ограничены Spotify Development Mode → 403 → внутренний retry-loop).
    # Раньше пытались через Lavalink, тратили 15-45с на таймауты — теперь не надо.
    if is_spotify_url:
        results = None
    else:
        results, search_error = await search_with_node_fallback(query, source)
        if search_error:
            log.warning("Все ноды дали ошибку при поиске: %s", search_error)

    # YouTube playlist fallback: если это ссылка на YT-плейлист и поиск упал
    # (классическая ошибка Lavalink при загрузке плейлистов), грузим вручную:
    # парсим страницу плейлиста YouTube → получаем video_id → ищем каждый трек
    yt_playlist_id = None
    if not results and ("youtube.com" in query.lower() or "youtu.be" in query.lower()):
        yt_playlist_id = parse_youtube_playlist_id(query)

    if yt_playlist_id:
        await msg.edit(content="📋 Получаю список треков из YouTube-плейлиста...")
        video_ids = await fetch_youtube_playlist_video_ids(
            yt_playlist_id, limit=PLAYLIST_TRACK_LIMIT
        )
        if not video_ids:
            await msg.edit(
                content="❗ Не удалось загрузить YouTube-плейлист.\n"
                        "_Возможно, он приватный или удалён._"
            )
            return

        if not await check_track_limit(interaction, len(video_ids)):
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            return

        player = await ensure_voice_connection(interaction)
        if player is None:
            await msg.edit(content="❗ Не удалось подключиться к голосовому каналу.")
            return

        await msg.edit(content=f"🔍 Загружаю {len(video_ids)} треков...")
        added_tracks = []
        for vid in video_ids:
            video_url = f"https://www.youtube.com/watch?v={vid}"
            yt_results, _ = await search_with_node_fallback(
                video_url, wavelink.TrackSource.YouTube
            )
            if yt_results:
                track = yt_results[0] if isinstance(yt_results, list) else yt_results.tracks[0]
                added_tracks.append(track)

        if not added_tracks:
            await msg.edit(content="😕 Не удалось загрузить ни одного трека из плейлиста.")
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
            await safe_play_track(player, first_track)
        else:
            await add_tracks_fairly(player, added_tracks, interaction.user.id, enabled=fair)
            await msg.edit(content=f"📋 Добавлено из YT-плейлиста: `{len(added_tracks)} треков`")
        increment_user_track_count(interaction.guild_id, interaction.user.id, len(added_tracks))
        return

    # Яндекс.Музыка не поддерживается (Lavalink её не знает, а прямой API
    # Яндекса блокирует все запросы извне РФ по HTTP 451)
    if not results and "music.yandex." in query.lower():
        await msg.edit(
            content="❌ **Яндекс.Музыка не поддерживается.**\n"
                    "_Причина: хостинг бота находится вне России, "
                    "и Яндекс блокирует запросы к своему API._\n\n"
                    "💡 **Что можно сделать:**\n"
                    "• Найти этот же трек в Spotify — `/play <ссылка Spotify>`\n"
                    "• Просто написать название трека — `/play Artist - Song`\n"
                    "• Использовать ссылку на YouTube"
        )
        return

    # Spotify обработка (всегда через нашу логику, минуя Lavalink+LavaSrc)
    # is_spotify_url был определён выше, тут просто проверяем
    if is_spotify_url:
        log.info("Spotify: пробую обработать %r", query)

        # Проверка что Spotify настроен
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            log.warning("Spotify: нет SPOTIFY_CLIENT_ID/SECRET в env")
            await msg.edit(
                content="❗ **Spotify не настроен на сервере бота.**\n"
                        "_Администратор должен добавить SPOTIFY_CLIENT_ID "
                        "и SPOTIFY_CLIENT_SECRET в переменные окружения._"
            )
            return

        # Проверка парсинга URL
        parsed = parse_spotify_url(query)
        if not parsed:
            log.warning("Spotify: не смог распарсить URL %r", query)
            await msg.edit(
                content="❗ **Неверный формат ссылки Spotify.**\n"
                        "_Поддерживаются: track, album, playlist._\n"
                        "Пример: `https://open.spotify.com/playlist/...`"
            )
            return
        log.info("Spotify: распознал тип=%s id=%s", parsed[0], parsed[1])

        await msg.edit(content="🎵 Получаю треки из Spotify через API...")
        spotify_tracks, sp_error = await fetch_spotify_with_fallback(query)
        if not spotify_tracks:
            log.warning("Spotify: ошибка %s", sp_error)

            # Подробное сообщение в зависимости от типа ошибки
            sp_type = parsed[0]
            type_name = {"track": "трек", "album": "альбом", "playlist": "плейлист"}.get(sp_type, "ресурс")

            if sp_error in ("playlist_not_found", "track_not_found", "album_not_found"):
                # Это самая частая причина — приватный плейлист
                err_msg = (
                    f"❌ **{type_name.capitalize()} недоступен через API.**\n\n"
                    "Возможные причины:\n"
                    f"• {type_name.capitalize()} **приватный** — Spotify API "
                    f"не отдаёт чужие приватные плейлисты\n"
                    f"• {type_name.capitalize()} удалён или его не существует\n"
                    "• Ссылка повреждена\n\n"
                    "💡 **Как проверить:**\n"
                    "1. Открой ссылку в режиме инкогнито (без логина)\n"
                    "2. Если видишь страницу — публичный, что-то ещё не так\n"
                    "3. Если просит залогиниться — приватный\n\n"
                    "Чтобы сделать плейлист публичным: открой его в Spotify → "
                    "три точки → «Сделать публичным»"
                )
            elif sp_error == "spotify_token_invalid":
                err_msg = (
                    "❌ **Spotify токен недействителен.**\n"
                    "_Администратору: проверь SPOTIFY_CLIENT_ID и "
                    "SPOTIFY_CLIENT_SECRET в Railway._"
                )
            elif sp_error == "spotify_network_error":
                err_msg = (
                    "❌ **Spotify API временно недоступен.**\n"
                    "_Попробуй ещё раз через минуту._"
                )
            elif sp_error == "spotify_empty":
                err_msg = (
                    f"❌ **{type_name.capitalize()} пустой.**\n"
                    "В нём нет треков, либо все треки недоступны в твоём регионе."
                )
            else:
                err_msg = (
                    f"❌ **Не удалось получить {type_name}.**\n"
                    f"_Ошибка: {sp_error}_"
                )
            await msg.edit(content=err_msg)
            return
        log.info("Spotify: получил %d треков", len(spotify_tracks))

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
            sp_results, _ = await search_with_node_fallback(
                f"{sp['artist']} - {sp['title']}",
                wavelink.TrackSource.YouTube,
            )
            if sp_results:
                track = sp_results[0] if isinstance(sp_results, list) else sp_results.tracks[0]
                added_tracks.append(track)
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
            await safe_play_track(player, first_track)
        else:
            await add_tracks_fairly(player, added_tracks, interaction.user.id, enabled=fair)
            await msg.edit(content=f"📋 Добавлено из Spotify: `{len(added_tracks)} треков`")
        increment_user_track_count(interaction.guild_id, interaction.user.id, len(added_tracks))
        return

    if not results:
        if search_error:
            err_str = str(search_error).lower()
            # Часто бывает что для одиночных треков всё ок, а для плейлистов
            # ломается из-за бага YouTube source плагина
            is_likely_playlist_bug = (
                "list=" in query.lower()
                or "timestamp" in err_str
                or "playlist" in err_str
            )
            if is_likely_playlist_bug:
                await msg.edit(
                    content="⚠️ **Не получилось загрузить плейлист.**\n"
                            "_Бесплатные Lavalink-ноды часто не справляются с YT-плейлистами._\n\n"
                            "💡 Попробуй:\n"
                            "• Скинуть ссылку на конкретное видео\n"
                            "• Использовать Spotify-плейлист\n"
                            "• Повторить через 1-2 минуты"
                )
            else:
                await msg.edit(
                    content="⚠️ **Lavalink-нода временно недоступна.**\n"
                            "_Это бывает с публичными нодами — YouTube периодически "
                            "блокирует их или они уходят на обслуживание._\n\n"
                            "💡 Попробуй:\n"
                            "• Повторить через 30-60 секунд\n"
                            "• Использовать прямую ссылку (YouTube/Spotify)\n"
                            "• Если не работает 5+ минут — скинь админу логи"
                )
        else:
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
            await safe_play_track(player, first_track)
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
    if not core.db_pool:
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

