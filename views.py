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

from i18n import t
from database import db_add_track, db_create_playlist, db_delete_track, db_get_playlist, db_get_settings, db_get_tracks, db_get_user_playlists, db_reset_settings, db_save_settings, db_update_track
from helpers import add_tracks_fairly, cancel_idle_timer, format_duration, full_disconnect, get_fair_queue_enabled, increment_user_track_count, is_dj, tag_track
from playback import connect_to_voice, search_with_node_fallback

# ─────────────────────────────────────────────
#  Голосование за скип
# ─────────────────────────────────────────────
BUTTON_LABELS = {
    "Пауза": "btn.pause", "Играть": "btn.play", "Скип": "btn.skip",
    "Повтор": "btn.loop", "Очередь": "btn.queue",
    "В плейлист": "btn.to_playlist", "Стоп": "btn.stop",
    "✅ За скип": "vote.yes_btn", "❌ Против": "vote.no_btn",
    "🔢 К странице": "queue.jump_btn", "📑 Компактно": "queue.compact",
}


def localize_buttons(view, guild_id):
    """Переводит подписи кнопок, заданных в декораторах (там нет guild)."""
    for child in view.children:
        key = BUTTON_LABELS.get(getattr(child, "label", None))
        if key:
            child.label = t(guild_id, key)


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
        localize_buttons(self, guild.id)
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
            t(self.guild.id, "vote.prompt", p0=len(self.votes_yes), p1=len(self.votes_no), p2=needed, p3=total)
        )

    @discord.ui.button(label="✅ За скип", style=discord.ButtonStyle.success)
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.channel or interaction.user not in player.channel.members:
            await interaction.response.send_message(
                t(interaction.guild_id, "vote.join_first"), ephemeral=True
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
                t(interaction.guild_id, "vote.join_first"), ephemeral=True
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
                await self.message.edit(content=t(self.guild.id, "vote.passed"), view=None)
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
                    content=t(self.guild.id, "vote.failed", p0=len(self.votes_yes)),
                    view=None
                )
            except discord.HTTPException:
                pass


# ─────────────────────────────────────────────
#  Пагинация очереди
# ─────────────────────────────────────────────
class JumpToPageModal(discord.ui.Modal, title="Go to page"):
    page_input = discord.ui.TextInput(
        label="Page number",
        placeholder="Enter a number",
        required=True,
        max_length=4,
    )

    def __init__(self, view: "QueuePaginationView"):
        super().__init__()
        self.view_ref = view
        gid = getattr(getattr(view, "guild", None), "id", None)
        self.title = t(gid, "jump.title")
        self.page_input.label = t(gid, "jump.label")
        self.page_input.placeholder = t(gid, "jump.placeholder")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = int((self.page_input.value or "").strip())
        except ValueError:
            await interaction.response.send_message(t(interaction.guild_id, "jump.need_number"), ephemeral=True)
            return
        total_pages = self.view_ref.total_pages()
        if target < 1 or target > total_pages:
            await interaction.response.send_message(
                t(interaction.guild_id, "jump.range", p0=total_pages), ephemeral=True
            )
            return
        self.view_ref.current_page = target - 1
        await interaction.response.edit_message(
            embed=self.view_ref.build_embed(),
            view=self.view_ref,
        )


class QueuePaginationView(discord.ui.View):
    def __init__(self, guild: discord.Guild, user_id: int):
        super().__init__(timeout=180)
        self.guild = guild
        self.user_id = user_id
        self.current_page = 0
        self.compact = False
        self.message: Optional[discord.Message] = None
        localize_buttons(self, guild.id)

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item: discord.ui.Item):
        if isinstance(error, discord.NotFound):
            log.debug("Протухшая кнопка пагинации очереди")
            return
        log.error("QueuePaginationView error: %s", error, exc_info=True)

    @property
    def player(self) -> Optional[wavelink.Player]:
        return self.guild.voice_client

    @property
    def per_page(self) -> int:
        return 20 if self.compact else TRACKS_PER_PAGE

    def get_queue_snapshot(self) -> list:
        p = self.player
        if not p:
            return []
        return list(p.queue)

    def total_pages(self) -> int:
        snapshot = self.get_queue_snapshot()
        if not snapshot:
            return 1
        return max(1, (len(snapshot) + self.per_page - 1) // self.per_page)

    def build_embed(self) -> discord.Embed:
        p = self.player
        snapshot = self.get_queue_snapshot()
        total_pages = self.total_pages()
        self.current_page = max(0, min(self.current_page, total_pages - 1))

        embed = discord.Embed(color=BRAND_COLOR)
        embed.set_author(name=t(self.guild.id, "queue.title"))

        if p and p.current:
            tr = p.current
            cur = f"[{tr.title}]({tr.uri})" if tr.uri else tr.title
            embed.add_field(
                name=t(self.guild.id, "queue.now"),
                value=f"**{cur}** `[{format_duration(t.length)}]`",
                inline=False,
            )

        if not snapshot:
            embed.description = t(self.guild.id, "queue.empty")
            self._update_buttons(total_pages)
            return embed

        start = self.current_page * self.per_page
        end = start + self.per_page
        shown = snapshot[start:end]

        lines = []
        for i, tr in enumerate(shown, start=start + 1):
            if self.compact:
                title = tr.title if len(tr.title) <= 58 else tr.title[:57] + "…"
                lines.append(f"`{i}.` {title}")
            else:
                link = t(self.guild.id, "queue.open", p0=tr.uri) if tr.uri else ""
                lines.append(f"`{i}.` {tr.title} `[{format_duration(tr.length)}]`{link}")
        embed.description = "\n".join(lines)
        embed.set_footer(
            text=t(self.guild.id, "queue.footer", p0=len(snapshot), p1=self.current_page + 1, p2=total_pages))

        self._update_buttons(total_pages)
        return embed

    def _update_buttons(self, total_pages: int):
        self.first_btn.disabled = self.current_page == 0
        self.prev_btn.disabled = self.current_page == 0
        self.next_btn.disabled = self.current_page >= total_pages - 1
        self.last_btn.disabled = self.current_page >= total_pages - 1
        self.jump_btn.disabled = total_pages <= 1
        self.compact_btn.label = t(self.guild.id, "queue.detailed") if self.compact else t(self.guild.id, "queue.compact")

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.total_pages() - 1, self.current_page + 1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = self.total_pages() - 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="🔢 К странице", style=discord.ButtonStyle.success)
    async def jump_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(JumpToPageModal(self))

    @discord.ui.button(label="📑 Компактно", style=discord.ButtonStyle.secondary)
    async def compact_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.compact = not self.compact
        self.current_page = 0
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ─────────────────────────────────────────────
#  Модалка добавления в плейлист
# ─────────────────────────────────────────────
class AddToPlaylistModal(discord.ui.Modal, title="Add to playlist"):
    playlist_name = discord.ui.TextInput(
        label="Playlist name",
        placeholder="Enter a playlist name",
        required=True,
        max_length=PLAYLIST_NAME_MAX,
    )

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild
        # подписи модалки задаются на уровне класса, где ещё нет guild —
        # поэтому переводим здесь, при создании
        self.title = t(guild.id, "addpl.modal_title")
        self.playlist_name.label = t(guild.id, "addpl.name_label")
        self.playlist_name.placeholder = t(guild.id, "addpl.name_ph")

    async def on_submit(self, interaction: discord.Interaction):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.current:
            await interaction.response.send_message(t(interaction.guild_id, "err.nothing_playing"), ephemeral=True)
            return
        if not core.db_pool:
            await interaction.response.send_message(t(interaction.guild_id, "err.no_db"), ephemeral=True)
            return
        name = (self.playlist_name.value or "").strip()
        if not name:
            await interaction.response.send_message(t(interaction.guild_id, "err.name_empty"), ephemeral=True)
            return
        playlist = await db_get_playlist(interaction.user.id, name)
        if playlist:
            pid = playlist["id"]
        else:
            pid = await db_create_playlist(interaction.user.id, name)
            if pid is None:
                await interaction.response.send_message(t(interaction.guild_id, "addpl.create_failed"), ephemeral=True)
                return
        track = player.current
        await db_add_track(pid, track.title, track.uri or "", track.length)
        await interaction.response.send_message(
            t(interaction.guild_id, "addpl.added_named", p0=track.title, p1=name), ephemeral=True
        )


class AddToPlaylistView(discord.ui.View):
    def __init__(self, guild: discord.Guild, user_id: int, playlists: list):
        super().__init__(timeout=60)
        self.guild = guild
        self.user_id = user_id
        options = []
        for p in playlists[:24]:
            options.append(discord.SelectOption(
                label=p["name"][:100],
                value=str(p["id"]),
                description=t(guild.id, "addpl.count", p0=p['track_count'])[:100],
            ))
        options.append(discord.SelectOption(
            label=t(guild.id, "addpl.create_new"), value="__new__", emoji="➕"))
        sel = discord.ui.Select(placeholder=t(guild.id, "addpl.pick"), options=options)
        sel.callback = self._on_select
        self.add_item(sel)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(t(interaction.guild_id, "err.not_your_menu"), ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction):
        val = interaction.data["values"][0]
        if val == "__new__":
            await interaction.response.send_modal(AddToPlaylistModal(self.guild))
            return
        player = self.guild.voice_client
        if not player or not player.current:
            await interaction.response.edit_message(content=t(interaction.guild_id, "err.nothing_playing"), view=None)
            return
        track = player.current
        await db_add_track(int(val), track.title, track.uri or "", track.length)
        await interaction.response.edit_message(
            content=t(interaction.guild_id, "addpl.added", p0=track.title), view=None)


# ─────────────────────────────────────────────
#  Кнопки управления под "Сейчас играет"
# ─────────────────────────────────────────────
class PlayerControls(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild
        localize_buttons(self, guild.id)

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
                    t(interaction.guild_id, "btn.error"), ephemeral=True
                )
        except discord.HTTPException:
            pass

    @property
    def player(self) -> Optional[wavelink.Player]:
        return self.guild.voice_client

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary, row=0, label="Пауза")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message(t(interaction.guild_id, "btn.dj_required"), ephemeral=True)
            return
        p = self.player
        if p and p.playing and not p.paused:
            await p.pause(True)
            button.emoji = "▶️"
            button.label = t(interaction.guild_id, "btn.play")
            await interaction.response.edit_message(view=self)
        elif p and p.paused:
            await p.pause(False)
            button.emoji = "⏸"
            button.label = t(interaction.guild_id, "btn.pause")
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
            await interaction.response.send_message(t(interaction.guild_id, "btn.dj_required"), ephemeral=True)
            return
        if p.queue.mode == wavelink.QueueMode.loop:
            p.queue.mode = wavelink.QueueMode.normal
        await p.skip(force=True)
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0, label="Повтор")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message(t(interaction.guild_id, "btn.dj_required"), ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        modes = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
        labels = {
            wavelink.QueueMode.normal:   t(interaction.guild_id, "btn.loop_off"),
            wavelink.QueueMode.loop:     t(interaction.guild_id, "btn.loop_track"),
            wavelink.QueueMode.loop_all: t(interaction.guild_id, "btn.loop_queue"),
        }
        current = p.queue.mode
        next_mode = modes[(modes.index(current) + 1) % 3]
        p.queue.mode = next_mode
        await interaction.response.send_message(labels[next_mode], ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0, label="Shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message(t(interaction.guild_id, "btn.dj_required"), ephemeral=True)
            return
        p = self.player
        if p and len(p.queue) > 1:
            p.queue.shuffle()
            await interaction.response.send_message(t(interaction.guild_id, "ctl.shuffled"), ephemeral=True)
        else:
            await interaction.response.send_message(t(interaction.guild_id, "err.nothing_to_shuffle"), ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=0, label="Очередь")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if not p or (not p.current and p.queue.is_empty):
            await interaction.response.send_message(t(interaction.guild_id, "queue.empty"), ephemeral=True)
            return
        view = QueuePaginationView(self.guild, interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1, label="-10%")
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message(t(interaction.guild_id, "btn.dj_required"), ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        new_vol = max(0, p.volume - 10)
        await p.set_volume(new_vol)
        await interaction.response.send_message(t(interaction.guild_id, "btn.vol_down", p0=new_vol), ephemeral=True)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1, label="+10%")
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message(t(interaction.guild_id, "btn.dj_required"), ephemeral=True)
            return
        p = self.player
        if not p:
            await interaction.response.defer()
            return
        new_vol = min(100, p.volume + 10)
        await p.set_volume(new_vol)
        await interaction.response.send_message(t(interaction.guild_id, "btn.vol_up", p0=new_vol), ephemeral=True)

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.secondary, row=1, label="В плейлист")
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = self.guild.voice_client
        if not player or not player.current:
            await interaction.response.send_message(t(interaction.guild_id, "err.nothing_playing"), ephemeral=True)
            return
        if not core.db_pool:
            await interaction.response.send_message(t(interaction.guild_id, "err.no_db"), ephemeral=True)
            return
        playlists = await db_get_user_playlists(interaction.user.id)
        if not playlists:
            await interaction.response.send_modal(AddToPlaylistModal(self.guild))
            return
        view = AddToPlaylistView(self.guild, interaction.user.id, playlists)
        await interaction.response.send_message(t(interaction.guild_id, "btn.pick_playlist"), view=view, ephemeral=True)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=1, label="Стоп")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_dj(interaction.user):
            await interaction.response.send_message(t(interaction.guild_id, "btn.dj_required"), ephemeral=True)
            return
        await full_disconnect(self.guild)
        await interaction.response.send_message(t(interaction.guild_id, "ctl.stopped"), ephemeral=True)


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
        cancel = discord.ui.Button(label=t(guild.id, "sel.cancel"), style=discord.ButtonStyle.danger)
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
                t(interaction.guild_id, "sel.not_yours"), ephemeral=True
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
                                content=t(interaction.guild_id, "sel.connect_failed"),
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
                    await self.search_msg.edit(content=t(interaction.guild_id, "sel.connect_error", p0=e), view=None)
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
                        content=t(interaction.guild_id, "play.added", p0=track.title, p1=format_duration(track.length)),
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
            await self.search_msg.edit(content=t(interaction.guild_id, "sel.cancelled"), view=None)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self):
        try:
            await self.search_msg.edit(content=t(self.guild.id, "sel.timeout"), view=None)
        except discord.HTTPException:
            pass


class PlaylistEditView(discord.ui.View):
    PER_PAGE = 25

    def __init__(self, owner_id, playlist_id, playlist_name, tracks, message=None,
                 guild_id=None):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.playlist_id = playlist_id
        self.playlist_name = playlist_name
        self.tracks = tracks
        self.page = 0
        self.selected_id = None
        self.message = message
        self._build()

    @property
    def pages(self):
        return max(1, (len(self.tracks) + self.PER_PAGE - 1) // self.PER_PAGE)

    def _build(self):
        self.clear_items()
        if not self.tracks:
            return
        start = self.page * self.PER_PAGE
        page_tracks = self.tracks[start:start + self.PER_PAGE]
        options = []
        for tr in page_tracks:
            options.append(discord.SelectOption(
                label=(tr["title"] or "—")[:100],
                value=str(tr["id"]),
                description=f"#{tr['position']} · {format_duration(tr['duration'])}"[:100],
                default=(tr["id"] == self.selected_id),
            ))
        sel = discord.ui.Select(placeholder=t(self.guild_id, "edit.pick_track"), options=options, row=0)
        sel.callback = self._on_select
        self.add_item(sel)
        if self.pages > 1:
            prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary,
                                     disabled=(self.page == 0), row=1)
            prev.callback = self._prev
            self.add_item(prev)
            nxt = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary,
                                    disabled=(self.page >= self.pages - 1), row=1)
            nxt.callback = self._next
            self.add_item(nxt)
        dele = discord.ui.Button(label=t(self.guild_id, "edit.delete"), style=discord.ButtonStyle.danger,
                                 disabled=(self.selected_id is None), row=1)
        dele.callback = self._delete
        self.add_item(dele)
        ver = discord.ui.Button(label=t(self.guild_id, "edit.change_version"), style=discord.ButtonStyle.primary,
                                disabled=(self.selected_id is None), row=1)
        ver.callback = self._change_version
        self.add_item(ver)

    def _text(self):
        head = t(self.guild_id, "edit.header", p0=self.playlist_name, p1=len(self.tracks))
        if self.pages > 1:
            head += t(self.guild_id, "edit.page", p0=self.page + 1, p1=self.pages)
        if self.selected_id:
            tr = next((x for x in self.tracks if x["id"] == self.selected_id), None)
            if tr:
                head += t(self.guild_id, "edit.selected", p0=tr["title"])
        return head

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(t(interaction.guild_id, "edit.not_yours"), ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction):
        self.selected_id = int(interaction.data["values"][0])
        self._build()
        await interaction.response.edit_message(content=self._text(), view=self)

    async def _prev(self, interaction):
        self.page = max(0, self.page - 1)
        self.selected_id = None
        self._build()
        await interaction.response.edit_message(content=self._text(), view=self)

    async def _next(self, interaction):
        self.page = min(self.pages - 1, self.page + 1)
        self.selected_id = None
        self._build()
        await interaction.response.edit_message(content=self._text(), view=self)

    async def _delete(self, interaction):
        if self.selected_id is None:
            return
        await db_delete_track(self.selected_id, self.playlist_id)
        self.tracks = await db_get_tracks(self.playlist_id)
        self.selected_id = None
        if self.page >= self.pages:
            self.page = self.pages - 1
        self._build()
        if not self.tracks:
            await interaction.response.edit_message(content=t(interaction.guild_id, "edit.now_empty"), view=None)
            self.stop()
            return
        await interaction.response.edit_message(content=self._text(), view=self)

    async def _change_version(self, interaction):
        if self.selected_id is None:
            return
        tr = next((x for x in self.tracks if x["id"] == self.selected_id), None)
        if not tr:
            return
        await interaction.response.defer()
        results, _ = await search_with_node_fallback(t["title"], wavelink.TrackSource.YouTube)
        if results:
            if isinstance(results, wavelink.Playlist):
                cand = results.tracks[:5]
            elif isinstance(results, list):
                cand = results[:5]
            else:
                cand = [results]
        else:
            cand = []
        if not cand:
            await interaction.followup.send(t(interaction.guild_id, "edit.no_versions"), ephemeral=True)
            return
        vview = VersionSelectView(self, t, cand)
        await interaction.edit_original_response(
            content=t(interaction.guild_id, "edit.pick_version", p0=tr['title']), view=vview)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content=t(self.guild_id, "edit.closed"), view=None)
            except discord.HTTPException:
                pass


class VersionSelectView(discord.ui.View):
    def __init__(self, editor, track_row, candidates):
        super().__init__(timeout=120)
        self.editor = editor
        self.track_row = track_row
        self.candidates = candidates
        options = []
        for i, c in enumerate(candidates):
            options.append(discord.SelectOption(
                label=c.title[:100],
                value=str(i),
                description=format_duration(c.length)[:100],
            ))
        sel = discord.ui.Select(placeholder=t(self.editor.guild_id, "ver.pick"), options=options, row=0)
        sel.callback = self._on_pick
        self.add_item(sel)
        cancel = discord.ui.Button(label=t(self.editor.guild_id, "ver.cancel"), style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.editor.owner_id:
            await interaction.response.send_message(t(interaction.guild_id, "edit.not_yours"), ephemeral=True)
            return False
        return True

    async def _on_pick(self, interaction):
        c = self.candidates[int(interaction.data["values"][0])]
        await db_update_track(self.track_row["id"], self.editor.playlist_id,
                              c.title, c.uri or "", c.length)
        self.editor.tracks = await db_get_tracks(self.editor.playlist_id)
        self.editor.selected_id = None
        self.editor._build()
        await interaction.response.edit_message(
            content=self.editor._text() + t(interaction.guild_id, "edit.version_updated", p0=c.title),
            view=self.editor)

    async def _cancel(self, interaction):
        self.editor._build()
        await interaction.response.edit_message(content=self.editor._text(), view=self.editor)


PANEL_TOGGLES = {
    "vote_skip_enabled": "panel.o_voteskip",
    "fair_queue": "panel.o_fair",
    "announce_now_playing": "panel.o_announce",
}
PANEL_NUMBERS = {
    "default_volume": ("panel.o_volume", 1, 100),
    "vote_skip_percent": ("panel.o_threshold", 1, 100),
    "track_limit": ("panel.o_limit", 0, 50),
    "idle_timeout": ("panel.o_idle", 60, 3600),
    "empty_timeout": ("panel.o_empty", 10, 3600),
}
PANEL_CHOICES = {
    "language": (
        "panel.o_language",
        ["ru", "en"],
        {"ru": "Русский", "en": "English"},
    ),
    "default_search_source": (
        "panel.o_source",
        ["youtube", "yandex", "soundcloud"],
        {"youtube": "YouTube", "yandex": "Yandex Music", "soundcloud": "SoundCloud"},
    ),
}
PANEL_MAIN = ["vote_skip_enabled", "fair_queue", "announce_now_playing", "default_volume", "default_search_source", "language"]
PANEL_ADVANCED = ["vote_skip_percent", "track_limit", "idle_timeout", "empty_timeout"]


class SettingValueModal(discord.ui.Modal):
    def __init__(self, panel, key):
        label_key, lo, hi = PANEL_NUMBERS[key]
        label = t(panel.guild.id, label_key)
        super().__init__(title=label[:45])
        self.panel = panel
        self.key = key
        self.lo, self.hi = lo, hi
        self.field = discord.ui.TextInput(
            label=t(self.guild.id, "panel.number_range", p0=lo, p1=hi), required=True, max_length=6)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v = int(self.field.value)
        except ValueError:
            await interaction.response.send_message(t(interaction.guild_id, "panel.need_int"), ephemeral=True)
            return
        v = max(self.lo, min(v, self.hi))
        await db_save_settings(self.panel.guild.id, **{self.key: v})
        await self.panel.refresh(interaction)


class SettingsPanelView(discord.ui.View):
    def __init__(self, guild: discord.Guild, settings: dict):
        super().__init__(timeout=300)
        self.guild = guild
        self.settings = settings
        self.show_all = False
        self._build()

    def _build(self):
        self.clear_items()
        rs = discord.ui.RoleSelect(placeholder=t(self.guild.id, "panel.pick_dj"),
                                   min_values=0, max_values=1, row=0)
        rs.callback = self._on_role
        self.add_item(rs)

        keys = PANEL_MAIN + (PANEL_ADVANCED if self.show_all else [])
        options = []
        for k in keys:
            if k in PANEL_TOGGLES:
                val = self.settings.get(k, False)
                options.append(discord.SelectOption(
                    label=t(self.guild.id, PANEL_TOGGLES[k]), value=k,
                    description=t(self.guild.id, "panel.now_on") if val else t(self.guild.id, "panel.now_off")))
            elif k in PANEL_NUMBERS:
                label_key, lo, hi = PANEL_NUMBERS[k]
                options.append(discord.SelectOption(
                    label=t(self.guild.id, label_key), value=k,
                    description=t(self.guild.id, "panel.now_value", p0=self.settings.get(k, lo))))
            else:
                label_key, opts, labels = PANEL_CHOICES[k]
                cur = self.settings.get(k, opts[0])
                options.append(discord.SelectOption(
                    label=t(self.guild.id, label_key), value=k,
                    description=t(self.guild.id, "panel.now_value", p0=labels.get(cur, cur))))
        sel = discord.ui.Select(placeholder=t(self.guild.id, "panel.change"), options=options, row=1)
        sel.callback = self._on_setting
        self.add_item(sel)

        btn = discord.ui.Button(
            label=t(self.guild.id, "panel.collapse") if self.show_all else t(self.guild.id, "panel.expand"),
            style=discord.ButtonStyle.secondary, row=2)
        btn.callback = self._toggle_all
        self.add_item(btn)
        reset = discord.ui.Button(label=t(self.guild.id, "panel.reset_dj"),
                                  style=discord.ButtonStyle.secondary, row=2)
        reset.callback = self._reset_dj
        self.add_item(reset)
        reset_all = discord.ui.Button(label=t(self.guild.id, "panel.reset_all"),
                                      style=discord.ButtonStyle.danger, row=2)
        reset_all.callback = self._reset_all
        self.add_item(reset_all)

    def build_embed(self):
        s = self.settings
        dj = f"<@&{s.get('dj_role_id')}>" if s.get("dj_role_id") else t(self.guild.id, "panel.not_set")
        vs = (t(self.guild.id, "panel.on_threshold", p0=s.get('vote_skip_percent', 50))
              if s.get("vote_skip_enabled") else t(self.guild.id, "panel.off"))
        tl = s.get("track_limit", 0)
        embed = discord.Embed(
            title=t(self.guild.id, "panel.title"),
            description=t(self.guild.id, "panel.desc"),
            color=BRAND_COLOR)
        embed.add_field(name=t(self.guild.id, "panel.f_dj"), value=dj, inline=True)
        embed.add_field(name=t(self.guild.id, "panel.f_voteskip"), value=vs, inline=True)
        embed.add_field(name=t(self.guild.id, "panel.f_fair"),
                        value=t(self.guild.id, "panel.on_f") if s.get("fair_queue") else t(self.guild.id, "panel.off_f"), inline=True)
        embed.add_field(name=t(self.guild.id, "panel.f_announce"),
                        value=t(self.guild.id, "panel.yes") if s.get("announce_now_playing", True) else t(self.guild.id, "panel.no"), inline=True)
        embed.add_field(name=t(self.guild.id, "panel.f_limit"),
                        value=t(self.guild.id, "panel.no_limit") if not tl else str(tl), inline=True)
        embed.add_field(name=t(self.guild.id, "panel.f_volume"),
                        value=f"{s.get('default_volume', 100)}%", inline=True)
        embed.add_field(name=t(self.guild.id, "panel.f_idle"),
                        value=t(self.guild.id, "panel.seconds", p0=s.get('idle_timeout', 300)), inline=True)
        embed.add_field(name=t(self.guild.id, "panel.f_empty"),
                        value=t(self.guild.id, "panel.seconds", p0=s.get('empty_timeout', 60)), inline=True)
        _src = {"youtube": "YouTube", "yandex": t(self.guild.id, "panel.yandex"), "soundcloud": "SoundCloud"}
        embed.add_field(name=t(self.guild.id, "panel.f_source"),
                        value=_src.get(s.get("default_search_source", "youtube"), "YouTube"), inline=True)
        embed.set_footer(text=t(self.guild.id, "panel.footer"))
        return embed

    async def interaction_check(self, interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                t(interaction.guild_id, "panel.need_perm"), ephemeral=True)
            return False
        return True

    async def refresh(self, interaction):
        self.settings = await db_get_settings(self.guild.id)
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_role(self, interaction):
        vals = interaction.data.get("values") or []
        role_id = int(vals[0]) if vals else None
        await db_save_settings(self.guild.id, dj_role_id=role_id)
        await self.refresh(interaction)

    async def _reset_dj(self, interaction):
        await db_save_settings(self.guild.id, dj_role_id=None)
        await self.refresh(interaction)

    async def _toggle_all(self, interaction):
        self.show_all = not self.show_all
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_setting(self, interaction):
        key = interaction.data["values"][0]
        if key in PANEL_TOGGLES:
            new = not self.settings.get(key, False)
            await db_save_settings(self.guild.id, **{key: new})
            await self.refresh(interaction)
        elif key in PANEL_NUMBERS:
            await interaction.response.send_modal(SettingValueModal(self, key))
        else:
            label_key, opts, labels = PANEL_CHOICES[key]
            label = t(self.guild.id, label_key)
            cur = self.settings.get(key, opts[0])
            options = [
                discord.SelectOption(label=labels.get(o, o), value=o,
                                     default=(o == cur))
                for o in opts
            ]
            sel = discord.ui.Select(placeholder=label[:100], options=options)

            async def _pick(inner, _key=key):
                await db_save_settings(self.guild.id,
                                       **{_key: inner.data["values"][0]})
                self.settings = await db_get_settings(self.guild.id)
                self._build()
                await inner.response.edit_message(
                    embed=self.build_embed(), view=self)

            sel.callback = _pick
            picker = discord.ui.View(timeout=60)
            picker.add_item(sel)
            back = discord.ui.Button(label=t(self.guild.id, "panel.back"),
                                     style=discord.ButtonStyle.secondary)

            async def _back(inner):
                self._build()
                await inner.response.edit_message(embed=self.build_embed(), view=self)

            back.callback = _back
            picker.add_item(back)
            embed = discord.Embed(
                title=f"⚙️ {label}",
                description=t(self.guild.id, "panel.pick_value"),
                color=BRAND_COLOR)
            await interaction.response.edit_message(embed=embed, view=picker)

    async def _reset_all(self, interaction):
        embed = discord.Embed(
            title=t(interaction.guild_id, "panel.reset_confirm"),
            description=t(interaction.guild_id, "panel.reset_desc"),
            color=BRAND_COLOR)
        await interaction.response.edit_message(embed=embed, view=ResetConfirmView(self))


class ResetConfirmView(discord.ui.View):
    def __init__(self, panel):
        super().__init__(timeout=60)
        self.panel = panel

    async def interaction_check(self, interaction):
        return await self.panel.interaction_check(interaction)

    @discord.ui.button(label="✅ Да, сбросить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_reset_settings(self.panel.guild.id)
        self.panel.settings = await db_get_settings(self.panel.guild.id)
        self.panel.show_all = False
        self.panel._build()
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self.panel)

    @discord.ui.button(label="↩️ Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self.panel)
