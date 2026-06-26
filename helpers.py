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

from database import db_get_settings

# ─────────────────────────────────────────────
#  Проверка прав DJ
# ─────────────────────────────────────────────
async def is_dj(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if not core.db_pool:
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
    if not core.db_pool:
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
    if not core.db_pool:
        return False
    settings = await db_get_settings(guild_id)
    return settings.get("fair_queue", False)


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

