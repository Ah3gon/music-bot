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

from database import db_add_track, db_create_playlist, db_delete_playlist, db_get_birthday, db_get_playlist, db_get_playlist_by_share_code, db_get_settings, db_get_tracks, db_get_user_playlists, db_save_settings, db_set_birthday, db_set_share_code
from helpers import add_tracks_fairly, check_track_limit, format_duration, get_fair_queue_enabled, increment_user_track_count, tag_track
from playback import detect_source_from_url, ensure_voice_connection, safe_play_track, search_with_node_fallback
from views import PlaylistEditView
from spotify import fetch_spotify_with_fallback, parse_spotify_url

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
    if not core.db_pool:
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
    if not core.db_pool:
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
    if not core.db_pool:
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
    if not core.db_pool:
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
    if not core.db_pool:
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
    if not core.db_pool:
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


async def _playlist_name_autocomplete(interaction: discord.Interaction, current: str):
    """Подсказывает плейлисты пользователя при вводе названия."""
    if not core.db_pool:
        return []
    try:
        playlists = await db_get_user_playlists(interaction.user.id)
    except Exception:
        return []
    cur = current.lower()
    out = []
    for p in playlists:
        if cur in p["name"].lower():
            label = f"{p['name']} ({p['track_count']} треков)"
            out.append(app_commands.Choice(name=label[:100], value=p["name"]))
        if len(out) >= 25:
            break
    return out


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
    if not core.db_pool:
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
    if not core.db_pool:
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
@app_commands.autocomplete(name=_playlist_name_autocomplete)
async def pl_play(interaction: discord.Interaction, name: str):
    if not core.db_pool:
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
        results, _ = await search_with_node_fallback(
            t["uri"], wavelink.TrackSource.YouTube
        )
        if results:
            track = results[0] if isinstance(results, list) else results.tracks[0]
            loaded.append(track)

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
        await safe_play_track(player, first)
    else:
        await add_tracks_fairly(player, loaded, interaction.user.id, enabled=fair)
        await msg.edit(content=f"📋 **{clean}** добавлен — `{len(loaded)} треков`")
    increment_user_track_count(interaction.guild_id, interaction.user.id, len(loaded))


@playlist_group.command(name="delete", description="Удалить плейлист")
@app_commands.describe(name="Название")
@app_commands.autocomplete(name=_playlist_name_autocomplete)
async def pl_delete(interaction: discord.Interaction, name: str):
    if not core.db_pool:
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
@app_commands.autocomplete(name=_playlist_name_autocomplete)
async def pl_addtrack(interaction: discord.Interaction, name: str):
    if not core.db_pool:
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
@app_commands.autocomplete(name=_playlist_name_autocomplete)
async def pl_tracks(interaction: discord.Interaction, name: str):
    if not core.db_pool:
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


@playlist_group.command(name="import", description="Импортировать плейлист по ссылке в свой плейлист")
@app_commands.describe(url="Ссылка (YouTube / Spotify / Яндекс / SoundCloud)", name="Название плейлиста в боте")
async def pl_import(interaction: discord.Interaction, url: str, name: str):
    if not core.db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.response.send_message(
            f"❗ Название должно быть не пустым и не длиннее {PLAYLIST_NAME_MAX} символов.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)

    # получить или создать плейлист
    playlist = await db_get_playlist(interaction.user.id, clean)
    if playlist:
        playlist_id = playlist["id"]
    else:
        playlist_id = await db_create_playlist(interaction.user.id, clean)
        if playlist_id is None:
            await interaction.followup.send("❗ Не удалось создать плейлист.", ephemeral=True)
            return

    url = url.strip()
    is_spotify_url = "spotify.com" in url.lower() or url.lower().startswith("spotify:")
    added = 0

    # ---- Spotify: получаем треки и ищем их на YouTube ----
    if is_spotify_url:
        if not parse_spotify_url(url):
            await interaction.followup.send("❗ Не похоже на ссылку Spotify.", ephemeral=True)
            return
        msg = await interaction.followup.send("🎵 Получаю треки из Spotify...", ephemeral=True, wait=True)
        spotify_tracks, sp_error = await fetch_spotify_with_fallback(url)
        if not spotify_tracks:
            await msg.edit(content=f"❌ Не удалось получить треки из Spotify (_{sp_error}_).")
            return
        limited = spotify_tracks[:PLAYLIST_TRACK_LIMIT]
        await msg.edit(content=f"🔍 Импортирую {len(limited)} треков (ищу на YouTube)...")
        for i, sp in enumerate(limited, 1):
            results, _ = await search_with_node_fallback(
                f"{sp['artist']} - {sp['title']}", wavelink.TrackSource.YouTube
            )
            if results:
                track = results[0] if isinstance(results, list) else results.tracks[0]
                await db_add_track(playlist_id, track.title, track.uri or "", track.length)
                added += 1
            if i % 25 == 0:
                try:
                    await msg.edit(content=f"🔍 Импортирую... {i}/{len(limited)}")
                except discord.HTTPException:
                    pass
        note = "" if added == len(spotify_tracks) else f"\n_Spotify отдал {len(spotify_tracks)} треков (лимит/ограничение API)._"
        await msg.edit(content=f"✅ Импортировано в **{clean}**: `{added}` треков из Spotify.{note}")
        return

    # ---- YouTube / Яндекс / SoundCloud: грузим через Lavalink напрямую ----
    source = detect_source_from_url(url)
    msg = await interaction.followup.send("⏳ Загружаю плейлист...", ephemeral=True, wait=True)
    results, err = await search_with_node_fallback(url, source)
    if not results:
        await msg.edit(content="😕 Ничего не найдено по ссылке. Возможно, это не плейлист, либо нода временно недоступна.")
        return
    if isinstance(results, wavelink.Playlist):
        tracks = results.tracks[:PLAYLIST_TRACK_LIMIT]
        total = len(results.tracks)
    elif isinstance(results, list):
        tracks = results[:PLAYLIST_TRACK_LIMIT]
        total = len(results)
    else:
        tracks = [results]
        total = 1
    for t in tracks:
        await db_add_track(playlist_id, t.title, t.uri or "", t.length)
        added += 1
    note = f"\n_Загружено {added} из {total} (лимит {PLAYLIST_TRACK_LIMIT})._" if total > PLAYLIST_TRACK_LIMIT else ""
    await msg.edit(content=f"✅ Импортировано в **{clean}**: `{added}` треков.{note}")


@playlist_group.command(name="edit", description="Редактировать плейлист: удалить трек или сменить версию")
@app_commands.describe(name="Название плейлиста")
@app_commands.autocomplete(name=_playlist_name_autocomplete)
async def pl_edit(interaction: discord.Interaction, name: str):
    if not core.db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.response.send_message("❗ Неверное название.", ephemeral=True)
        return
    playlist = await db_get_playlist(interaction.user.id, clean)
    if not playlist:
        await interaction.response.send_message(f"❗ Плейлист **{clean}** не найден.", ephemeral=True)
        return
    tracks = await db_get_tracks(playlist["id"])
    if not tracks:
        await interaction.response.send_message(f"📭 Плейлист **{clean}** пуст.", ephemeral=True)
        return
    view = PlaylistEditView(interaction.user.id, playlist["id"], clean, tracks)
    await interaction.response.send_message(view._text(), view=view, ephemeral=True)
    view.message = await interaction.original_response()


@playlist_group.command(name="share", description="Поделиться плейлистом — получить код")
@app_commands.describe(name="Название плейлиста")
@app_commands.autocomplete(name=_playlist_name_autocomplete)
async def pl_share(interaction: discord.Interaction, name: str):
    if not core.db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    clean = _validate_playlist_name(name)
    if not clean:
        await interaction.response.send_message("❗ Неверное название.", ephemeral=True)
        return
    playlist = await db_get_playlist(interaction.user.id, clean)
    if not playlist:
        await interaction.response.send_message(f"❗ Плейлист **{clean}** не найден.", ephemeral=True)
        return
    code = playlist.get("share_code")
    if not code:
        for _ in range(10):
            cand = base64.urlsafe_b64encode(os.urandom(6)).decode().rstrip("=")
            if not await db_get_playlist_by_share_code(cand):
                code = cand
                break
        if not code:
            await interaction.response.send_message("❗ Не удалось сгенерировать код, попробуй ещё раз.", ephemeral=True)
            return
        await db_set_share_code(playlist["id"], code)
    await interaction.response.send_message(
        f"🔗 Код плейлиста **{clean}**: `{code}`\n"
        f"Любой может добавить его себе: `/playlist import-shared {code}`\n"
        f"_Делятся снимком треков — твои дальнейшие изменения копию не затронут._",
        ephemeral=True,
    )


@playlist_group.command(name="import-shared", description="Добавить чужой плейлист по коду")
@app_commands.describe(code="Код, которым с тобой поделились", name="(необязательно) название для копии")
async def pl_import_shared(interaction: discord.Interaction, code: str, name: Optional[str] = None):
    if not core.db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    src = await db_get_playlist_by_share_code(code.strip())
    if not src:
        await interaction.followup.send("❗ Плейлист с таким кодом не найден.", ephemeral=True)
        return
    tracks = await db_get_tracks(src["id"])
    if not tracks:
        await interaction.followup.send("📭 Этот плейлист пуст — нечего копировать.", ephemeral=True)
        return
    base = (_validate_playlist_name(name) if name else None) or src["name"]
    target = base
    new_id = await db_create_playlist(interaction.user.id, target)
    n = 2
    while new_id is None and n < 50:
        target = f"{base} ({n})"
        new_id = await db_create_playlist(interaction.user.id, target)
        n += 1
    if new_id is None:
        await interaction.followup.send("❗ Не удалось создать плейлист.", ephemeral=True)
        return
    added = 0
    for t in tracks:
        await db_add_track(new_id, t["title"], t["uri"] or "", t["duration"])
        added += 1
    await interaction.followup.send(
        f"✅ Плейлист скопирован как **{target}** — `{added}` треков. Играй: `/playlist play {target}`",
        ephemeral=True,
    )


tree.add_command(playlist_group)


# ─────────────────────────────────────────────
#  День рождения
# ─────────────────────────────────────────────
birthday_group = app_commands.Group(name="birthday", description="День рождения")


@birthday_group.command(name="set", description="Установить свой день рождения")
@app_commands.describe(day="День (1-31)", month="Месяц (1-12)")
async def birthday_set(interaction: discord.Interaction, day: int, month: int):
    if not core.db_pool:
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
    if not core.db_pool:
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
    async with core.db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE birthdays SET birthday_song=$1 WHERE user_id=$2",
            query, interaction.user.id
        )
    await interaction.response.send_message(
        f"🎵 Поздравительная песня: **{query}**", ephemeral=True
    )


@birthday_group.command(name="remove", description="Удалить свой день рождения")
async def birthday_remove(interaction: discord.Interaction):
    if not core.db_pool:
        await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
        return
    async with core.db_pool.acquire() as conn:
        await conn.execute("DELETE FROM birthdays WHERE user_id=$1", interaction.user.id)
    await interaction.response.send_message("✅ День рождения удалён.", ephemeral=True)


tree.add_command(birthday_group)


# ─────────────────────────────────────────────
#  Help
# ─────────────────────────────────────────────
HELP_CATEGORIES = {
    "play": ("▶️", "Воспроизведение", """`/play <запрос/ссылка>` — YouTube, SoundCloud, Spotify, Яндекс
`/skip` — пропустить (или голосование за скип)
`/skipto <номер>` — перейти к треку очереди
`/forward <сек>` / `/rewind <сек>` — перемотка
`/pause` — пауза / продолжить
`/stop` — остановить и выйти
`/volume <0-100>` — громкость
`/loop` — режим повтора
`/shuffle` — перемешать очередь"""),
    "queue": ("📋", "Очередь", """`/queue` — показать очередь (есть компактный режим)
`/nowplaying` — что сейчас играет
`/remove <номер>` — убрать трек из очереди
`/history` — недавно сыгранные треки
`/stats` — статистика прослушивания
`/savequeue <название>` — сохранить очередь как плейлист"""),
    "playlists": ("💾", "Плейлисты", """`/playlist create <название>` — создать плейлист
`/playlist list` — твои плейлисты
`/playlist play <название>` — включить плейлист
`/playlist tracks <название>` — показать треки
`/playlist addtrack <название>` — добавить текущий трек
`/playlist delete <название>` — удалить плейлист
`/playlist import <ссылка> <название>` — импорт по ссылке
`/playlist edit <название>` — редактор: удалить трек / сменить версию
`/playlist share <название>` — поделиться (получить код)
`/playlist import-shared <код>` — добавить чужой плейлист по коду"""),
    "fx": ("✨", "Эффекты и текст", """`/effect <эффект>` — bassboost, nightcore, vaporwave, slowmo, 8d
`/lyrics` — текст текущей песни"""),
    "settings": ("⚙️", "Настройки сервера (админ)", """`/settings show` — текущие настройки
`/settings djrole <роль>` / `djrole_remove` — DJ-роль
`/settings voteskip <вкл/выкл> [%]` — голосование за скип
`/settings tracklimit <число>` — лимит треков на человека
`/settings fairqueue <вкл/выкл>` — справедливая очередь"""),
    "birthday": ("🎂", "День рождения", """`/birthday set` — установить свой день рождения
`/birthday song` — выбрать поздравительный трек
`/birthday remove` — убрать
Бот поздравит именинника при заходе в голосовой канал"""),
    "buttons": ("🎮", "Кнопки под «Сейчас играет»", """⏸ пауза · ⏭ скип · 🔁 повтор · 🔀 shuffle · 📋 очередь
🔉 −10% · 🔊 +10% · 💾 в плейлист · ⏹ стоп"""),
}


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        options = [
            discord.SelectOption(label=title, value=key, emoji=emoji)
            for key, (emoji, title, _) in HELP_CATEGORIES.items()
        ]
        sel = discord.ui.Select(placeholder="Выбери категорию команд…", options=options)
        sel.callback = self._on_select
        self.add_item(sel)

    @staticmethod
    def main_embed():
        embed = discord.Embed(
            title=f"🎵 {BOT_NAME} — помощь",
            description="Выбери категорию в меню ниже, чтобы увидеть команды.",
            color=BRAND_COLOR,
        )
        cats = " · ".join(f"{e} {t}" for e, t, _ in HELP_CATEGORIES.values())
        embed.add_field(name="Категории", value=cats, inline=False)
        embed.set_footer(text=BOT_NAME)
        return embed

    async def _on_select(self, interaction):
        emoji, title, content = HELP_CATEGORIES[interaction.data["values"][0]]
        embed = discord.Embed(title=f"{emoji} {title}", description=content, color=BRAND_COLOR)
        embed.set_footer(text=f"{BOT_NAME} • /help")
        await interaction.response.edit_message(embed=embed, view=self)


@tree.command(name="help", description="Список всех команд")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=HelpView.main_embed(), view=HelpView(), ephemeral=True)


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

