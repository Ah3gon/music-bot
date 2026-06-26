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

from database import db_get_settings, db_get_stats
from helpers import apply_effect, check_dj, format_duration, full_disconnect, is_dj, make_progress_bar, now_playing_embed
from lyrics import fetch_lyrics
from views import QueuePaginationView, start_vote_skip

# ─────────────────────────────────────────────
#  Управление плеером
# ─────────────────────────────────────────────
@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not (player.playing or player.paused):
        await interaction.response.send_message("❗ Ничего не играет.")
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
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
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
    await interaction.response.send_message(embed=now_playing_embed(t, player, player.position, current_effect.get(interaction.guild.id, "off")))


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
    if not core.db_pool:
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

