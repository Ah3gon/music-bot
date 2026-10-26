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

from database import db_get_birthday, db_get_settings, db_increment_stats, db_increment_user_stats, db_load_languages, init_db
from helpers import add_to_history, cancel_empty_channel_timer, cancel_idle_timer, full_disconnect, get_track_owner, is_birthday_today, now_playing_embed, start_idle_timer, tag_track
from playback import connect_to_voice, find_alternative_track, guild_search_source, pick_full_track, safe_play_track, search_with_node_fallback
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
    cancel_idle_timer(guild.id)
    state = get_player_state(guild.id)
    # consec_fails здесь НЕ сбрасываем: упавший трек сначала стартует, потом
    # падает. Сброс — в track_end, когда трек доиграл или его пропустили.
    # Счётчик подмен сбрасываем только когда заиграл ДРУГОЙ трек:
    # подменённая версия имеет то же название и стартует перед падением,
    # а найденную замену (название часто другое) узнаём по fallback_alt.
    is_alt = state.pop("fallback_alt", None) == track.encoded
    if not is_alt and state.get("fallback_title") != track.title:
        state["fallback_tries"] = 0
        state.pop("fallback_title", None)

    is_birthday_track = (track is not None
                         and state.get("birthday_track") == track.encoded)
    # История и статистика пишутся в track_end — только для сыгранных треков

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
    reason = str(getattr(payload, "reason", "") or "").lower()
    # replaced — трек перебит новым play(), очередь двигать нельзя;
    # cleanup — плеер уничтожен.
    if reason in ("replaced", "cleanup"):
        return
    # Трек упал: очередь продвигает on_wavelink_track_exception (он приходит
    # раньше и может ещё искать замену). Иначе оба обработчика запускают
    # по треку и перебивают друг друга, съедая очередь.
    owned = state.get("fail_owned")
    if (owned and payload.track is not None
            and owned[0] == payload.track.encoded
            and time.monotonic() - owned[1] < 30):
        state.pop("fail_owned", None)
        return
    if reason in ("finished", "stopped"):
        state["consec_fails"] = 0
        # Трек реально играл (доиграл или его пропустили) — в историю и
        # статистику. Упавшие сюда не доходят: их забирает fail_owned выше.
        await _record_played(guild, payload.track)

    channel_id = state.get("text_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None

    # День рождения. Треки узнаём по encoded, а не по флагу: иначе конец
    # прерванного трека (его пропускают ради поздравления) принимался за
    # конец самого поздравления.
    enc = payload.track.encoded if payload.track is not None else None
    bday_interrupted = enc is not None and state.get("birthday_interrupted") == enc
    bday_ended = enc is not None and state.get("birthday_track") == enc
    if bday_interrupted:
        state.pop("birthday_interrupted", None)
    if bday_ended:
        state.pop("birthday_track", None)
        resume = state.pop("birthday_resume", None)
        prev_mode = state.pop("birthday_prev_mode", None)
        if prev_mode is not None:
            player.queue.mode = prev_mode
        if resume and not player.queue.is_empty:
            seek_to = resume["position"]

            async def _seek_after_start():
                await asyncio.sleep(0.8)
                p: wavelink.Player = guild.voice_client
                if p and p.current:
                    try:
                        await p.seek(min(seek_to, p.current.length))
                    except Exception as e:
                        log.debug("Birthday seek error: %s", e)
            asyncio.create_task(_seek_after_start())
    if bday_interrupted or bday_ended:
        # Ни поздравление, ни прерванный трек не повторяем: прерванный уже
        # стоит в очереди сразу после поздравления
        await _play_next(player, guild, state, channel)
        return

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

    await _play_next(player, guild, state, channel)


async def _record_played(guild: discord.Guild, track):
    if track is None:
        return
    add_to_history(guild.id, track)
    if not core.db_pool:
        return
    try:
        await db_increment_stats(guild.id, track.length)
        owner = get_track_owner(guild.id, track)
        if owner:
            await db_increment_user_stats(owner, track.length, getattr(track, "author", "") or "")
    except Exception as e:
        log.warning("Stats increment error: %s", e)


async def _play_next(player: wavelink.Player, guild: discord.Guild,
                     state: dict, channel):
    """Запускает следующий трек очереди (с предохранителем) или idle-таймер."""
    if state.get("consec_fails", 0) >= 4 and not player.queue.is_empty:
        dropped = len(player.queue)
        player.queue.clear()
        state["consec_fails"] = 0
        log.warning("Предохранитель: 4 трека подряд не запустились, очередь (%d) очищена", dropped)
        if channel:
            try:
                await channel.send(
                    f"⚠️ Несколько треков подряд не запустились — источник сейчас недоступен.\n"
                    f"Очистил очередь ({dropped} шт.), попробуй позже или другой источник."
                )
            except discord.HTTPException:
                pass
            await start_idle_timer(guild, channel)
        return

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
    guild = player.guild
    state = get_player_state(guild.id)
    channel_id = state.get("text_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None

    if track is None:
        state["consec_fails"] = state.get("consec_fails", 0) + 1
        return  # не знаем, какой трек упал — очередь продвинет track_end

    # Продвижение очереди берём на себя — track_end этого трека пропустит.
    # Ставим метку до первого await, чтобы track_end её точно увидел.
    # В режиме повтора упавший трек тоже не перезапускаем — иначе вечный цикл.
    state["fail_owned"] = (track.encoded, time.monotonic())

    # Источник недоступен (частая беда с YouTube) — ищем тот же трек
    # на резервных источниках и играем его вместо упавшего.
    tried = state.get("fallback_tries", 0)
    if not state.get("fallback_in_progress") and tried < 2:
        state["fallback_tries"] = tried + 1
        state["fallback_title"] = track.title
        state["fallback_in_progress"] = True
        try:
            alt = await find_alternative_track(
                track.title, getattr(track, "author", "") or "",
                exclude_source=str(getattr(track, "source", "") or ""))
        except Exception as e:
            alt = None
            log.warning("Поиск замены не удался: %s", e)
        finally:
            state["fallback_in_progress"] = False
        if _other_track_started(player, track):
            return  # пока искали, кто-то включил другой трек — не перебиваем
        if alt is not None:
            log.info("Замена источника: '%s' → '%s'", track.title, alt.title)
            owner = get_track_owner(guild.id, track)
            if owner:
                tag_track(guild.id, alt, owner)
            state["source_swapped"] = True
            state["fallback_alt"] = alt.encoded
            if await safe_play_track(player, alt):
                if channel:
                    try:
                        await channel.send(
                            f"🔄 **{track.title}** недоступен на исходном источнике — играю найденную версию."
                        )
                    except discord.HTTPException:
                        pass
                return
            state.pop("fallback_alt", None)

    state["consec_fails"] = state.get("consec_fails", 0) + 1
    if _other_track_started(player, track):
        return
    await _play_next(player, guild, state, channel)


def _other_track_started(player: wavelink.Player, failed) -> bool:
    """Играет уже не упавший трек, а какой-то другой (например, новый /play)."""
    cur = player.current
    return cur is not None and cur.encoded != failed.encoded


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
        song_query, await guild_search_source(member.guild.id)
    )
    if err:
        log.warning("Birthday search error: %s", err)
    if not results:
        return
    found = results if isinstance(results, list) else results.tracks
    bday_track = pick_full_track(found)
    if bday_track is None:
        return

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
            state["birthday_track"] = bday_track.encoded
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

    state["birthday_track"] = bday_track.encoded
    current_track = player.current
    if current_track is None:
        await player.queue.put_wait(bday_track)
        if not player.playing:
            await player.play(player.queue.get())
        return

    state["birthday_resume"] = {"position": player.position}

    prev_mode = player.queue.mode
    if prev_mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal

    try:
        # Прерванный трек ставим сразу за поздравлением и доигрываем с той же
        # позиции. Искать его заново не нужно — берём тот же объект.
        try:
            player.queue.put_at(0, bday_track)
            player.queue.put_at(1, current_track)
        except AttributeError:
            current_queue = list(player.queue)
            player.queue.clear()
            await player.queue.put_wait(bday_track)
            await player.queue.put_wait(current_track)
            for t in current_queue:
                await player.queue.put_wait(t)

        state["birthday_prev_mode"] = prev_mode
        state["birthday_interrupted"] = current_track.encoded
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
        for k in ("birthday_resume", "birthday_track", "birthday_interrupted",
                  "birthday_prev_mode"):
            state.pop(k, None)
        player.queue.mode = prev_mode


# Кого уже поздравили сегодня: {guild_id: {user_id}}. Отдельно от player_state,
# потому что full_disconnect его очищает — и поздравление звучало повторно.
_birthday_greeted: dict[int, set] = {}


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
    already = _birthday_greeted.setdefault(member.guild.id, set())
    if member.id in already:
        return
    already.add(member.id)
    await _play_birthday_now(member, channel, birthday)


@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
    if bot.user and member.id == bot.user.id:
        # Бота отключили от голоса вручную (кик, удалили канал) — чистим
        # очередь, таймеры и состояние, иначе они висят до рестарта
        if before.channel and not after.channel:
            await full_disconnect(member.guild)
        return
    if member.bot:
        return

    if after.channel and not before.channel:
        await _handle_birthday(member, after.channel)

    player: wavelink.Player = member.guild.voice_client
    if not player or not player.channel:
        return
    if after.channel and after.channel == player.channel:
        # Реагируем только на реальный вход в канал (а не мьют/анмьют и т.п.)
        if before.channel != after.channel:
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
        # Снимаем себя из словаря ДО full_disconnect: иначе он через
        # cancel_empty_channel_timer отменит эту же задачу и оборвёт отключение.
        if empty_channel_tasks.get(guild.id) is asyncio.current_task():
            empty_channel_tasks.pop(guild.id, None)
        p: wavelink.Player = guild.voice_client
        if not p or not p.connected or not p.channel:
            return
        current_non_bots = [m for m in p.channel.members if not m.bot]
        if len(current_non_bots) > 0:
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
        _birthday_greeted.clear()
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
            await db_load_languages()
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


