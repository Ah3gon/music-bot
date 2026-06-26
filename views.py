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

from database import db_add_track, db_get_playlist, db_get_settings
from helpers import add_tracks_fairly, cancel_idle_timer, format_duration, full_disconnect, get_fair_queue_enabled, increment_user_track_count, is_dj, tag_track
from playback import connect_to_voice

# ─────────────────────────────────────────────
#  Голосование за скип
# ─────────────────────────────────────────────
class VoteSkipView(discord.ui.View):
    def __init__(self, guild: discord.Guild, channel: discord.TextChannel,
                 required_percent: int, initiator: discord.Member):
        super().__init__(timeout=30)
        self.guild = guild
        self.channel = channel
        self.required_percent = required_percent
        self.initiator = initiator
        self.votes_yes: set[int] = {initiator.id}
        self.votes_no: set[int] = set()
        self.message: Optional[discord.Message] = None
        self.resolved = False

    def get_voter_count(self) -> int:
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.channel:
            return 1
        return max(1, len([m for m in player.channel.members if not m.bot]))

    def is_passed(self) -> bool:
        total = self.get_voter_count()
        needed = max(1, int(total * self.required_percent / 100))
        return len(self.votes_yes) >= needed

    def status_text(self) -> str:
        total = self.get_voter_count()
        needed = max(1, int(total * self.required_percent / 100))
        return (
            f"⏭ **Голосование за скип**\n"
            f"✅ За: **{len(self.votes_yes)}** | ❌ Против: **{len(self.votes_no)}**\n"
            f"Нужно **{needed}** из **{total}** голосов | Осталось 30 сек"
        )

    @discord.ui.button(label="✅ За скип", style=discord.ButtonStyle.success)
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.channel or interaction.user not in player.channel.members:
            await interaction.response.send_message(
                "❗ Войди в голосовой канал чтобы голосовать.", ephemeral=True
            )
            return
        self.votes_yes.add(interaction.user.id)
        self.votes_no.discard(interaction.user.id)
        await interaction.response.edit_message(content=self.status_text(), view=self)
        if self.is_passed():
            await self.do_skip()

    @discord.ui.button(label="❌ Против", style=discord.ButtonStyle.danger)
    async def vote_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.channel or interaction.user not in player.channel.members:
            await interaction.response.send_message(
                "❗ Войди в голосовой канал чтобы голосовать.", ephemeral=True
            )
            return
        self.votes_no.add(interaction.user.id)
        self.votes_yes.discard(interaction.user.id)
        await interaction.response.edit_message(content=self.status_text(), view=self)

    async def do_skip(self):
        if self.resolved:
            return
        self.resolved = True
        self.stop()
        active_votes.pop(self.guild.id, None)
        player: wavelink.Player = self.guild.voice_client
        if player and (player.playing or player.paused):
            try:
                if player.queue.mode == wavelink.QueueMode.loop:
                    player.queue.mode = wavelink.QueueMode.normal
                await player.skip(force=True)
            except Exception as e:
                log.warning("Skip error: %s", e)
        if self.message:
            try:
                await self.message.edit(content="✅ Голосование прошло — трек пропущен!", view=None)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        if self.resolved:
            return
        self.resolved = True
        active_votes.pop(self.guild.id, None)
        if self.message:
            try:
                await self.message.edit(
                    content=f"❌ Голосование завершилось — недостаточно голосов "
                            f"({len(self.votes_yes)} за, нужно было больше).",
                    view=None
                )
            except discord.HTTPException:
                pass


# ─────────────────────────────────────────────
#  Пагинация очереди
# ─────────────────────────────────────────────
class JumpToPageModal(discord.ui.Modal, title="Перейти к странице"):
    page_input = discord.ui.TextInput(
        label="Номер страницы",
        placeholder="Введи число",
        required=True,
        max_length=4,
    )

    def __init__(self, view: "QueuePaginationView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = int((self.page_input.value or "").strip())
        except ValueError:
            await interaction.response.send_message("❗ Введи число.", ephemeral=True)
            return
        total_pages = self.view_ref.total_pages()
        if target < 1 or target > total_pages:
            await interaction.response.send_message(
                f"❗ Номер от 1 до {total_pages}.", ephemeral=True
            )
            return
        self.view_ref.current_page = target - 1
        await interaction.response.edit_message(
            content=self.view_ref.build_text(),
            view=self.view_ref,
        )


class QueuePaginationView(discord.ui.View):
    def __init__(self, guild: discord.Guild, user_id: int):
        super().__init__(timeout=180)
        self.guild = guild
        self.user_id = user_id
        self.current_page = 0
        self.message: Optional[discord.Message] = None

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item: discord.ui.Item):
        if isinstance(error, discord.NotFound):
            log.debug("Протухшая кнопка пагинации очереди")
            return
        log.error("QueuePaginationView error: %s", error, exc_info=True)

    @property
    def player(self) -> Optional[wavelink.Player]:
        return self.guild.voice_client

    def get_queue_snapshot(self) -> list:
        p = self.player
        if not p:
            return []
        return list(p.queue)

    def total_pages(self) -> int:
        snapshot = self.get_queue_snapshot()
        if not snapshot:
            return 1
        return max(1, (len(snapshot) + TRACKS_PER_PAGE - 1) // TRACKS_PER_PAGE)

    def build_text(self) -> str:
        p = self.player
        snapshot = self.get_queue_snapshot()
        total_pages = self.total_pages()
        self.current_page = max(0, min(self.current_page, total_pages - 1))

        lines = []
        if p and p.current:
            t = p.current
            link = f" — [открыть]({t.uri})" if t.uri else ""
            lines.append(
                f"🎵 **Сейчас:** {t.title} `[{format_duration(t.length)}]`{link}\n"
            )

        if not snapshot:
            lines.append("📭 Очередь пуста.")
            self._update_buttons(total_pages)
            return "\n".join(lines)

        start = self.current_page * TRACKS_PER_PAGE
        end = start + TRACKS_PER_PAGE
        shown = snapshot[start:end]

        lines.append(
            f"**Очередь** — `{len(snapshot)} треков` · "
            f"Страница **{self.current_page + 1}/{total_pages}**\n"
        )
        for i, t in enumerate(shown, start=start + 1):
            link = f" — [открыть]({t.uri})" if t.uri else ""
            lines.append(f"`{i}.` {t.title} `[{format_duration(t.length)}]`{link}")

        self._update_buttons(total_pages)
        return "\n".join(lines)

    def _update_buttons(self, total_pages: int):
        self.first_btn.disabled = self.current_page == 0
        self.prev_btn.disabled = self.current_page == 0
        self.next_btn.disabled = self.current_page >= total_pages - 1
        self.last_btn.disabled = self.current_page >= total_pages - 1
        self.jump_btn.disabled = total_pages <= 1

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await interaction.response.edit_message(content=self.build_text(), view=self, suppress_embeds=True)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(content=self.build_text(), view=self, suppress_embeds=True)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.total_pages() - 1, self.current_page + 1)
        await interaction.response.edit_message(content=self.build_text(), view=self, suppress_embeds=True)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = self.total_pages() - 1
        await interaction.response.edit_message(content=self.build_text(), view=self, suppress_embeds=True)

    @discord.ui.button(label="🔢 К странице", style=discord.ButtonStyle.success)
    async def jump_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(JumpToPageModal(self))


# ─────────────────────────────────────────────
#  Модалка добавления в плейлист
# ─────────────────────────────────────────────
class AddToPlaylistModal(discord.ui.Modal, title="Добавить в плейлист"):
    playlist_name = discord.ui.TextInput(
        label="Название плейлиста",
        placeholder="Введи название плейлиста",
        required=True,
        max_length=PLAYLIST_NAME_MAX,
    )

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.current:
            await interaction.response.send_message("❗ Ничего не играет.", ephemeral=True)
            return
        if not core.db_pool:
            await interaction.response.send_message("❗ База данных недоступна.", ephemeral=True)
            return
        name = (self.playlist_name.value or "").strip()
        if not name:
            await interaction.response.send_message("❗ Название не может быть пустым.", ephemeral=True)
            return
        playlist = await db_get_playlist(interaction.user.id, name)
        if not playlist:
            await interaction.response.send_message(
                f"❗ Плейлист **{name}** не найден.", ephemeral=True
            )
            return
        track = player.current
        await db_add_track(playlist["id"], track.title, track.uri or "", track.length)
        await interaction.response.send_message(
            f"✅ **{track.title}** добавлен в **{name}**!", ephemeral=True
        )


# ─────────────────────────────────────────────
#  Кнопки управления под "Сейчас играет"
# ─────────────────────────────────────────────
class PlayerControls(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item: discord.ui.Item):
        """Перехватываем 404 Unknown interaction (протухшие кнопки)."""
        if isinstance(error, discord.NotFound):
            log.debug("Протухшая кнопка нажата (NotFound): %s", item)
            return
        log.error("PlayerControls button error: %s", error, exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❗ Ошибка кнопки. Попробуй позже.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    @property
    def player(self) -> Optional[wavelink.Player]:
        return self.guild.voice_client

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary, row=0, label="Пауза")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if p and p.playing and not p.paused:
            await p.pause(True)
            button.emoji = "▶️"
            button.label = "Играть"
            await interaction.response.edit_message(view=self)
        elif p and p.paused:
            await p.pause(False)
            button.emoji = "⏸"
            button.label = "Пауза"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, row=0, label="Скип")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if not p or not (p.playing or p.paused):
            await interaction.response.defer()
            return
        if core.db_pool:
            settings = await db_get_settings(self.guild.id)
            if settings.get("vote_skip_enabled") and not await is_dj(interaction.user):
                await interaction.response.defer()
                await start_vote_skip(
                    interaction.user, self.guild,
                    interaction.channel, settings.get("vote_skip_percent", 50)
                )
                return
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        if p.queue.mode == wavelink.QueueMode.loop:
            p.queue.mode = wavelink.QueueMode.normal
        await p.skip(force=True)
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0, label="Повтор")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        modes = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
        labels = {
            wavelink.QueueMode.normal:   "Повтор выкл ➡️",
            wavelink.QueueMode.loop:     "Повтор трека 🔂",
            wavelink.QueueMode.loop_all: "Повтор очереди 🔁",
        }
        current = p.queue.mode
        next_mode = modes[(modes.index(current) + 1) % 3]
        p.queue.mode = next_mode
        await interaction.response.send_message(labels[next_mode], ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0, label="Shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if p and len(p.queue) > 1:
            p.queue.shuffle()
            await interaction.response.send_message("🔀 Очередь перемешана.", ephemeral=True)
        else:
            await interaction.response.send_message("❗ Нечего перемешивать.", ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=0, label="Очередь")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if not p or (not p.current and p.queue.is_empty):
            await interaction.response.send_message("📭 Очередь пуста.", ephemeral=True)
            return
        view = QueuePaginationView(self.guild, interaction.user.id)
        text = view.build_text()
        await interaction.response.send_message(text, view=view, ephemeral=True, suppress_embeds=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1, label="-10%")
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        new_vol = max(0, p.volume - 10)
        await p.set_volume(new_vol)
        await interaction.response.send_message(f"🔉 Громкость: **{new_vol}%**", ephemeral=True)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1, label="+10%")
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        new_vol = min(100, p.volume + 10)
        await p.set_volume(new_vol)
        await interaction.response.send_message(f"🔊 Громкость: **{new_vol}%**", ephemeral=True)

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.secondary, row=1, label="В плейлист")
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddToPlaylistModal(self.guild))

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=1, label="Стоп")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message("❗ Нужна роль DJ.", ephemeral=True)
            return
        await full_disconnect(self.guild)
        await interaction.response.send_message("⏹ Остановлено.", ephemeral=True)


async def start_vote_skip(member: discord.Member, guild: discord.Guild,
                          channel: discord.TextChannel, percent: int):
    if guild.id in active_votes:
        view = active_votes[guild.id]
        view.votes_yes.add(member.id)
        view.votes_no.discard(member.id)
        if view.message:
            try:
                await view.message.edit(content=view.status_text(), view=view)
            except discord.HTTPException:
                pass
        if view.is_passed():
            await view.do_skip()
        return

    view = VoteSkipView(guild, channel, percent, member)
    active_votes[guild.id] = view
    try:
        msg = await channel.send(view.status_text(), view=view)
        view.message = msg
    except discord.HTTPException as e:
        log.warning("Vote skip send error: %s", e)
        active_votes.pop(guild.id, None)
        return

    if view.is_passed():
        await view.do_skip()


# ─────────────────────────────────────────────
#  View: выбор трека
# ─────────────────────────────────────────────
class TrackSelectView(discord.ui.View):
    def __init__(self, tracks: list, guild: discord.Guild,
                 voice_channel: discord.VoiceChannel,
                 text_channel: discord.TextChannel,
                 search_msg: discord.Message,
                 user_id: int):
        super().__init__(timeout=60)
        self.tracks = tracks
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.search_msg = search_msg
        self.user_id = user_id
        for i in range(len(tracks)):
            btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary)
            btn.callback = self._make_cb(i)
            self.add_item(btn)
        cancel = discord.ui.Button(label="✖ Отмена", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item: discord.ui.Item):
        if isinstance(error, discord.NotFound):
            log.debug("Протухшая кнопка выбора трека")
            return
        log.error("TrackSelectView error: %s", error, exc_info=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❗ Это меню выбора не для тебя.", ephemeral=True
            )
            return False
        return True

    def _make_cb(self, index: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            track = self.tracks[index]
            player: wavelink.Player = self.guild.voice_client
            try:
                if player is None:
                    player = await connect_to_voice(self.voice_channel)
                    if player is None:
                        try:
                            await self.search_msg.edit(
                                content="❗ Не удалось подключиться к голосовому каналу.\n"
                                        "_Lavalink-ноды могут быть недоступны или Discord "
                                        "не отвечает. Попробуй ещё раз через минуту._",
                                view=None,
                            )
                        except discord.HTTPException:
                            pass
                        self.stop()
                        return
                elif player.channel != self.voice_channel:
                    await player.move_to(self.voice_channel)
            except Exception as e:
                log.warning("Voice connect error: %s", e)
                try:
                    await self.search_msg.edit(content=f"❗ Не удалось подключиться: {e}", view=None)
                except discord.HTTPException:
                    pass
                self.stop()
                return
            player.autoplay = wavelink.AutoPlayMode.disabled
            get_player_state(self.guild.id)["text_channel_id"] = self.text_channel.id
            fair = await get_fair_queue_enabled(self.guild.id)
            if not player.playing:
                try:
                    await self.search_msg.delete()
                except discord.HTTPException:
                    pass
                increment_user_track_count(self.guild.id, self.user_id)
                tag_track(self.guild.id, track, self.user_id)
                await player.play(track)
            else:
                await add_tracks_fairly(player, [track], self.user_id, enabled=fair)
                increment_user_track_count(self.guild.id, self.user_id)
                try:
                    await self.search_msg.edit(
                        content=f"➕ **Добавлено:** {track.title} `[{format_duration(track.length)}]`",
                        view=None,
                    )
                except discord.HTTPException:
                    pass
            cancel_idle_timer(self.guild.id)
            self.stop()
        return callback

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self.search_msg.edit(content="❌ Отменено.", view=None)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self):
        try:
            await self.search_msg.edit(content="⏱ Время вышло.", view=None)
        except discord.HTTPException:
            pass

