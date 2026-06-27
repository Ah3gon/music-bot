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

from database import db_get_birthday, db_get_settings, db_increment_stats, init_db
from helpers import add_to_history, cancel_empty_channel_timer, cancel_idle_timer, full_disconnect, is_birthday_today, now_playing_embed, start_idle_timer
from playback import connect_to_voice, safe_play_track, search_with_node_fallback
from views import PlayerControls

# ─────────────────────────────────────────────
#  События Wavelink
# ─────────────────────────────────────────────
_ready_once = False

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

    if core.db_pool:
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

    settings = await db_get_settings(guild.id)
    if settings.get("announce_now_playing", True):
        effect = current_effect.get(guild.id, "off")
        embed = now_playing_embed(track, player, 0, effect, is_birthday_track)
        try:
            msg = await channel.send(embed=embed, view=PlayerControls(guild))
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
        ok = await safe_play_track(player, payload.track)
        if not ok:
            log.warning("Не удалось перезапустить трек в loop, выключаю режим")
            player.queue.mode = wavelink.QueueMode.normal
            # Пытаемся продолжить с очереди
            if not player.queue.is_empty:
                next_track = player.queue.get()
                await safe_play_track(player, next_track)
        return
    if player.queue.mode == wavelink.QueueMode.loop_all:
        try:
            await player.queue.put_wait(payload.track)
        except Exception:
            pass

    if not player.queue.is_empty:
        # Пробуем запустить следующие треки. Если первый не запустился,
        # пробуем второй, третий и т.д. — на случай "битых" треков
        max_skip_attempts = 3
        for skip_attempt in range(max_skip_attempts):
            if player.queue.is_empty:
                break
            next_track = player.queue.get()
            ok = await safe_play_track(player, next_track)
            if ok:
                break
            log.warning("Трек '%s' не запустился, пробую следующий", next_track.title)
        else:
            log.error("Не смог запустить ни один из %d следующих треков",
                      max_skip_attempts)
            if channel:
                try:
                    await channel.send(
                        "⚠️ Lavalink-нода вернулась с ошибкой.\n"
                        "_Попробуй `/play <трек>` чтобы перезапустить плеер._"
                    )
                except discord.HTTPException:
                    pass
                await start_idle_timer(guild, channel)
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


@bot.event
async def on_wavelink_node_disconnected(payload):
    """Сообщение в логах когда нода падает — поможет понять какие ноды надёжные."""
    node = getattr(payload, "node", None)
    log.warning("Lavalink node disconnected: %s", node)


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    """Трек упал с ошибкой во время игры — попробуем перейти к следующему."""
    player = payload.player
    if not player:
        return
    track = payload.track
    log.warning("Track exception: %s — '%s'",
                getattr(payload, "exception", "?"),
                track.title if track else "?")
    # Wavelink сам должен вызвать track_end после exception, но на всякий случай
    # пробуем запустить следующий
    if player.queue.is_empty:
        return
    if player.playing:
        return  # уже играет что-то
    next_track = player.queue.get()
    ok = await safe_play_track(player, next_track)
    if not ok:
        log.error("Не смог продолжить очередь после track exception")


@bot.event
async def on_wavelink_track_stuck(payload):
    """Трек завис (>10 сек без прогресса). Принудительно переходим дальше."""
    player = payload.player
    if not player:
        return
    log.warning("Track stuck — пропускаю и иду к следующему")
    try:
        if player.queue.mode == wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.normal
        await player.skip(force=True)
    except Exception as e:
        log.warning("Stuck skip error: %s", e)


# ─────────────────────────────────────────────
#  День рождения
# ─────────────────────────────────────────────
async def _play_birthday_now(member: discord.Member,
                              channel: discord.VoiceChannel,
                              birthday: dict):
    song_query = birthday.get("birthday_song") or "Happy Birthday instrumental"
    results, err = await search_with_node_fallback(
        song_query, wavelink.TrackSource.YouTube
    )
    if err:
        log.warning("Birthday search error: %s", err)
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
            player = await connect_to_voice(channel)
            if player is None:
                log.warning("Birthday: не удалось подключиться к голосовому каналу")
                return
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
        resume_results, _ = await search_with_node_fallback(
            current_track.uri or current_track.title,
            wavelink.TrackSource.YouTube,
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
    if not core.db_pool:
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
        empty = EMPTY_CH_TIMEOUT
        try:
            s = await db_get_settings(guild.id)
            empty = int(s.get("empty_timeout") or EMPTY_CH_TIMEOUT)
        except Exception:
            pass
        try:
            await asyncio.sleep(empty)
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
        log.info("Подключение к %d Lavalink-нодам инициировано", len(nodes))
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


