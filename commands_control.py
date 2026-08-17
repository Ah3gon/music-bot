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
import core
from core import *

from database import db_get_settings, db_get_stats, db_get_user_stats, db_get_user_top_artists
from helpers import apply_effect, check_dj, format_duration, full_disconnect, is_dj, now_playing_embed
from i18n import t
from lyrics import fetch_lyrics
from views import QueuePaginationView, start_vote_skip

# ─────────────────────────────────────────────
#  Управление плеером
# ─────────────────────────────────────────────
@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not (player.playing or player.paused):
        await interaction.response.send_message(t(interaction.guild_id, "err.nothing_playing"))
        return

    if core.db_pool:
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
        await interaction.response.send_message(t(interaction.guild_id, "ctl.skipped"))


@tree.command(name="skipto", description="Перейти к треку по номеру в очереди")
@app_commands.describe(position="Номер трека")
async def skipto_cmd(interaction: discord.Interaction, position: int):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message(t(interaction.guild_id, "err.not_in_channel"))
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(t(interaction.guild_id, "err.bad_position", max=len(q)))
        return
    for _ in range(position - 1):
        try:
            q.get()
        except Exception:
            break
    if player.queue.mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal
    await player.skip(force=True)
    await interaction.response.send_message(t(interaction.guild_id, "ctl.skipto", position=position))


async def _do_seek(interaction: discord.Interaction, delta_seconds: int):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message(t(interaction.guild_id, "err.nothing_playing"))
        return
    if player.current.length <= 0:
        await interaction.response.send_message(t(interaction.guild_id, "err.no_seek_live"))
        return
    if abs(delta_seconds) > SEEK_MAX_SECONDS:
        await interaction.response.send_message(
            t(interaction.guild_id, "err.seek_max", max=SEEK_MAX_SECONDS)
        )
        return
    new_pos = max(0, player.position + delta_seconds * 1000)
    new_pos = min(new_pos, player.current.length)
    await player.seek(new_pos)
    arrow = "⏩" if delta_seconds > 0 else "⏪"
    await interaction.response.send_message(
        t(interaction.guild_id, "ctl.seeked", arrow=arrow, pos=format_duration(new_pos),
          total=format_duration(player.current.length))
    )


@tree.command(name="forward", description="Перемотать вперёд на N секунд")
@app_commands.describe(seconds="Сколько секунд пропустить вперёд")
async def forward_cmd(interaction: discord.Interaction, seconds: int):
    if seconds <= 0:
        await interaction.response.send_message(t(interaction.guild_id, "err.positive_seconds"))
        return
    await _do_seek(interaction, seconds)


@tree.command(name="rewind", description="Перемотать назад на N секунд")
@app_commands.describe(seconds="Сколько секунд отмотать назад")
async def rewind_cmd(interaction: discord.Interaction, seconds: int):
    if seconds <= 0:
        await interaction.response.send_message(t(interaction.guild_id, "err.positive_seconds"))
        return
    await _do_seek(interaction, -seconds)


@tree.command(name="pause", description="Пауза / продолжить")
async def pause_cmd(interaction: discord.Interaction):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if player and player.playing and not player.paused:
        await player.pause(True)
        await interaction.response.send_message(t(interaction.guild_id, "ctl.paused"))
    elif player and player.paused:
        await player.pause(False)
        await interaction.response.send_message(t(interaction.guild_id, "ctl.resumed"))
    else:
        await interaction.response.send_message(t(interaction.guild_id, "err.nothing_playing"))


@tree.command(name="stop", description="Остановить и очистить очередь")
async def stop_cmd(interaction: discord.Interaction):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        await full_disconnect(interaction.guild)
        await interaction.response.send_message(t(interaction.guild_id, "ctl.stopped"))
    else:
        await interaction.response.send_message(t(interaction.guild_id, "err.not_in_channel"))


@tree.command(name="volume", description="Громкость от 0 до 100")
@app_commands.describe(level="Уровень громкости (0–100)")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if not await check_dj(interaction):
        return
    if not 0 <= level <= 100:
        await interaction.response.send_message(t(interaction.guild_id, "err.volume_range"))
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message(t(interaction.guild_id, "err.not_in_channel"))
        return
    await player.set_volume(level)
    await interaction.response.send_message(t(interaction.guild_id, "ctl.volume", level=level))


@tree.command(name="loop", description="Переключить режим повтора")
async def loop_cmd(interaction: discord.Interaction):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message(t(interaction.guild_id, "err.not_in_channel"))
        return
    modes = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
    labels = {
        wavelink.QueueMode.normal:   t(interaction.guild_id, "loop.off"),
        wavelink.QueueMode.loop:     t(interaction.guild_id, "loop.track"),
        wavelink.QueueMode.loop_all: t(interaction.guild_id, "loop.queue"),
    }
    current = player.queue.mode
    next_mode = modes[(modes.index(current) + 1) % 3]
    player.queue.mode = next_mode
    await interaction.response.send_message(t(interaction.guild_id, "ctl.loop", mode=labels[next_mode]))


@tree.command(name="shuffle", description="Перемешать очередь")
async def shuffle_cmd(interaction: discord.Interaction):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if player and len(player.queue) > 1:
        player.queue.shuffle()
        await interaction.response.send_message(t(interaction.guild_id, "ctl.shuffled"))
    else:
        await interaction.response.send_message(t(interaction.guild_id, "err.nothing_to_shuffle"))


@tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер трека в очереди")
async def remove_cmd(interaction: discord.Interaction, position: int):
    if not await check_dj(interaction):
        return
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message(t(interaction.guild_id, "err.not_in_channel"))
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(t(interaction.guild_id, "err.bad_position", max=len(q)))
        return
    all_tracks = list(q)
    removed = all_tracks.pop(position - 1)
    q.clear()
    for tr in all_tracks:
        await q.put_wait(tr)
    await interaction.response.send_message(t(interaction.guild_id, "ctl.removed", title=removed.title))


@tree.command(name="queue", description="Показать очередь с пагинацией")
async def queue_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or (not player.current and player.queue.is_empty):
        await interaction.response.send_message(t(interaction.guild_id, "queue.empty"))
        return
    view = QueuePaginationView(interaction.guild, interaction.user.id)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        pass


@tree.command(name="nowplaying", description="Что сейчас играет")
async def np_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message(t(interaction.guild_id, "queue.nothing"))
        return
    cur = player.current
    await interaction.response.send_message(embed=now_playing_embed(cur, player, player.position, current_effect.get(interaction.guild.id, "off")))


@tree.command(name="lyrics", description="Показать текст текущей песни")
async def lyrics_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message(t(interaction.guild_id, "err.nothing_playing"))
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
            t(interaction.guild_id, "lyrics.not_found", title=title, sources=", ".join(sources))
        )
        return

    text = result["text"]
    if len(text) > LYRICS_MAX_CHARS:
        text = text[:LYRICS_MAX_CHARS] + t(interaction.guild_id, "lyrics.truncated")

    synced_mark = " 🎤" if result.get("synced") else ""
    await interaction.followup.send(
        t(interaction.guild_id, "lyrics.header", title=title,
          source=result["source"], synced=synced_mark) + "\n\n" + text
    )


@tree.command(name="history", description="История последних треков")
async def history_cmd(interaction: discord.Interaction):
    history = track_history.get(interaction.guild_id, [])
    if not history:
        await interaction.response.send_message(t(interaction.guild_id, "queue.history_empty"), ephemeral=True)
        return
    lines = [t(interaction.guild_id, "queue.history_title") + "\n"]
    for i, tr in enumerate(history, 1):
        link = (" — [{}]({})".format(t(interaction.guild_id, "queue.open_link"), tr["uri"])
                if tr.get("uri") else "")
        lines.append(f"`{i}.` {tr['title']} `[{format_duration(tr['length'])}]`{link}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="stats", description="Статистика прослушивания на сервере")
async def stats_cmd(interaction: discord.Interaction):
    if not core.db_pool:
        await interaction.response.send_message(t(interaction.guild_id, "err.no_db"), ephemeral=True)
        return
    stats = await db_get_stats(interaction.guild_id)
    if not stats:
        await interaction.response.send_message(
            t(interaction.guild_id, "stats.empty"), ephemeral=True
        )
        return
    total_hours = stats["total_ms"] // 3600000
    total_minutes = (stats["total_ms"] % 3600000) // 60000
    embed = discord.Embed(title=t(interaction.guild_id, "stats.title", bot=BOT_NAME), color=BRAND_COLOR)
    embed.add_field(name=t(interaction.guild_id, "stats.tracks"),
                    value=f"**{stats['tracks_played']}**", inline=True)
    embed.add_field(name=t(interaction.guild_id, "stats.total_time"),
                    value=t(interaction.guild_id, "stats.duration", h=total_hours, m=total_minutes),
                    inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="mystats", description="Твоя личная статистика прослушивания")
async def mystats_cmd(interaction: discord.Interaction):
    if not core.db_pool:
        await interaction.response.send_message(t(interaction.guild_id, "err.no_db"), ephemeral=True)
        return
    stats = await db_get_user_stats(interaction.user.id)
    if not stats or not stats.get("tracks_played"):
        await interaction.response.send_message(
            t(interaction.guild_id, "mystats.empty"), ephemeral=True)
        return
    total_hours = stats["total_ms"] // 3600000
    total_minutes = (stats["total_ms"] % 3600000) // 60000
    top = await db_get_user_top_artists(interaction.user.id, 5)
    embed = discord.Embed(title=t(interaction.guild_id, "mystats.title"), color=BRAND_COLOR)
    embed.set_author(name=interaction.user.display_name,
                     icon_url=interaction.user.display_avatar.url)
    embed.add_field(name=t(interaction.guild_id, "stats.tracks"),
                    value=f"**{stats['tracks_played']}**", inline=True)
    embed.add_field(name=t(interaction.guild_id, "mystats.listen_time"),
                    value=t(interaction.guild_id, "stats.duration", h=total_hours, m=total_minutes),
                    inline=True)
    if top:
        lines = [f"`{i}.` {a['artist']} — {a['plays']}" for i, a in enumerate(top, 1)]
        embed.add_field(name=t(interaction.guild_id, "mystats.top_artists"),
                        value=chr(10).join(lines), inline=False)
    embed.set_footer(text=BOT_NAME)
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
        await interaction.response.send_message(t(interaction.guild_id, "err.not_in_channel"))
        return
    await interaction.response.defer()
    try:
        await apply_effect(player, effect.value)
        current_effect[interaction.guild_id] = effect.value
        if effect.value == "off":
            await interaction.followup.send(t(interaction.guild_id, "fx.off"))
        else:
            await interaction.followup.send(t(interaction.guild_id, "fx.applied", name=effect.name))
    except Exception as e:
        log.warning("Effect error: %s", e)
        await interaction.followup.send(t(interaction.guild_id, "err.error", error=e))

