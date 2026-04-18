import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import asyncio
import random
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────
DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
IDLE_TIMEOUT     = 300   # секунд тишины перед автовыходом
EMPTY_CH_TIMEOUT = 60    # секунд в пустом канале

NODES = [
    {"uri": "http://lavalink.jirayu.net:13592", "password": "youshallnotpass"},
    {"uri": "http://n3.nexcloud.in:2026",       "password": "nexcloud"},
]

# ─────────────────────────────────────────────
#  Бот
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

idle_tasks: dict[int, asyncio.Task] = {}


# ─────────────────────────────────────────────
#  Утилиты
# ─────────────────────────────────────────────
def format_duration(ms: int) -> str:
    seconds = ms // 1000
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


async def start_idle_timer(guild: discord.Guild, channel: discord.TextChannel):
    if guild.id in idle_tasks:
        idle_tasks[guild.id].cancel()

    async def _timer():
        await asyncio.sleep(IDLE_TIMEOUT)
        player: wavelink.Player = guild.voice_client
        # Исправлен баг: не выходим если трек на паузе
        if player and not player.playing and not player.paused:
            await player.disconnect()
            idle_tasks.pop(guild.id, None)
            await channel.send(f"💤 Вышел — {IDLE_TIMEOUT // 60} мин тишины.")

    idle_tasks[guild.id] = asyncio.create_task(_timer())


def cancel_idle_timer(guild_id: int):
    if guild_id in idle_tasks:
        idle_tasks[guild_id].cancel()
        del idle_tasks[guild_id]


# ─────────────────────────────────────────────
#  Модальное окно очереди
# ─────────────────────────────────────────────
class QueueModal(discord.ui.Modal, title="Очередь треков"):
    count = discord.ui.TextInput(
        label="Сколько треков показать?",
        placeholder="Оставь пустым чтобы показать все",
        required=False,
        max_length=3,
    )

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        player: wavelink.Player = self.guild.voice_client
        if not player or (not player.current and player.queue.is_empty):
            await interaction.response.send_message("📭 Очередь пуста.", ephemeral=True)
            return

        try:
            limit = int(self.count.value) if self.count.value.strip() else None
        except ValueError:
            await interaction.response.send_message("❗ Введи число.", ephemeral=True)
            return

        lines = []
        if player.current:
            t = player.current
            link = f" — [открыть]({t.uri})" if t.uri else ""
            lines.append(f"🎵 **Сейчас:** {t.title} `[{format_duration(t.length)}]`{link}\n")

        if not player.queue.is_empty:
            queue_list = list(player.queue)
            total = len(queue_list)
            shown = queue_list[:limit] if limit else queue_list[:20]
            lines.append("**В очереди:**")
            for i, t in enumerate(shown, 1):
                lines.append(f"`{i}.` 🎵 {t.title} `[{format_duration(t.length)}]`")
            if limit and total > limit:
                lines.append(f"_...и ещё {total - limit} треков_")
            elif not limit and total > 20:
                lines.append(f"_...и ещё {total - 20} треков (уточни число для просмотра)_")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ─────────────────────────────────────────────
#  Кнопки управления под "Сейчас играет"
# ─────────────────────────────────────────────
class PlayerControls(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild

    @property
    def player(self) -> Optional[wavelink.Player]:
        return self.guild.voice_client

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p and p.playing and not p.paused:
            await p.pause(True)
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
        elif p and p.paused:
            await p.pause(False)
            button.emoji = "⏸"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p and (p.playing or p.paused):
            await p.skip(force=True)
            await interaction.response.defer()
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
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

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p and len(p.queue) > 1:
            p.queue.shuffle()
            await interaction.response.send_message("🔀 Очередь перемешана.", ephemeral=True)
        else:
            await interaction.response.send_message("❗ Нечего перемешивать.", ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QueueModal(self.guild))

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p:
            p.queue.clear()
            cancel_idle_timer(self.guild.id)  # Исправлен баг: чистим таймер
            await p.stop()
            await p.disconnect()
        await interaction.response.send_message("⏹ Остановлено.", ephemeral=True)


# ─────────────────────────────────────────────
#  View: выбор трека из результатов поиска
# ─────────────────────────────────────────────
class TrackSelectView(discord.ui.View):
    def __init__(self, tracks: list[wavelink.Playable], guild: discord.Guild,
                 voice_channel: discord.VoiceChannel,
                 text_channel: discord.TextChannel,
                 search_msg: discord.Message):
        super().__init__(timeout=60)
        self.tracks = tracks
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.search_msg = search_msg

        for i in range(len(tracks)):
            btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary)
            btn.callback = self._make_cb(i)
            self.add_item(btn)

        cancel = discord.ui.Button(label="✖ Отмена", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _make_cb(self, index: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            track = self.tracks[index]

            player: wavelink.Player = self.guild.voice_client
            if player is None:
                player = await self.voice_channel.connect(cls=wavelink.Player)
            elif player.channel != self.voice_channel:
                await player.move_to(self.voice_channel)

            player.autoplay = wavelink.AutoPlayMode.disabled
            player._text_channel_id = self.text_channel.id

            if not player.playing:
                await self.search_msg.delete()
                await player.play(track)
            else:
                await player.queue.put_wait(track)
                await self.search_msg.edit(
                    content=f"➕ **Добавлено:** {track.title} `[{format_duration(track.length)}]`",
                    view=None
                )

            cancel_idle_timer(self.guild.id)
            self.stop()
        return callback

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.search_msg.edit(content="❌ Отменено.", view=None)
        self.stop()


# ─────────────────────────────────────────────
#  События Wavelink
# ─────────────────────────────────────────────
@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player = payload.player
    track = payload.track
    guild = player.guild

    channel_id = getattr(player, "_text_channel_id", None)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not channel:
        return

    msg_id = getattr(player, "_now_playing_msg_id", None)
    if msg_id:
        try:
            old_msg = await channel.fetch_message(msg_id)
            await old_msg.delete()
        except Exception:
            pass

    loop_labels = {
        wavelink.QueueMode.normal:   "выкл ➡️",
        wavelink.QueueMode.loop:     "трек 🔂",
        wavelink.QueueMode.loop_all: "очередь 🔁",
    }
    link = f" — [открыть]({track.uri})" if track.uri else ""
    text = (
        f"🎵 **Сейчас играет:** {track.title} "
        f"`[{format_duration(track.length)}]`{link}\n"
        f"Повтор: **{loop_labels[player.queue.mode]}**"
    )
    msg = await channel.send(text, view=PlayerControls(guild))
    player._now_playing_msg_id = msg.id


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    guild = player.guild

    channel_id = getattr(player, "_text_channel_id", None)
    channel = guild.get_channel(channel_id) if channel_id else None

    if player.queue.mode == wavelink.QueueMode.loop:
        await player.play(payload.track)
        return

    if player.queue.mode == wavelink.QueueMode.loop_all:
        await player.queue.put_wait(payload.track)

    if not player.queue.is_empty:
        next_track = player.queue.get()
        await player.play(next_track)
    elif channel:
        await start_idle_timer(guild, channel)


@bot.event
async def on_wavelink_inactive_player(player: wavelink.Player):
    cancel_idle_timer(player.guild.id)
    await player.disconnect()


# ─────────────────────────────────────────────
#  Slash-команды
# ─────────────────────────────────────────────
@tree.command(name="play", description="Поиск трека или добавление плейлиста с YouTube")
@app_commands.describe(query="Название трека, ссылка на видео или плейлист")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        await interaction.followup.send("❗ Зайди в голосовой канал сначала.")
        return

    msg = await interaction.followup.send(f"🔍 Ищу **{query}**...", wait=True)
    results = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)

    if not results:
        await msg.edit(content="😕 Ничего не найдено.")
        return

    # Подключаемся к каналу заранее
    player: wavelink.Player = interaction.guild.voice_client
    if player is None:
        player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
    elif player.channel != interaction.user.voice.channel:
        await player.move_to(interaction.user.voice.channel)

    player.autoplay = wavelink.AutoPlayMode.disabled
    player._text_channel_id = interaction.channel.id
    cancel_idle_timer(interaction.guild_id)

    # Если результат — плейлист
    if isinstance(results, wavelink.Playlist):
        for track in results.tracks:
            await player.queue.put_wait(track)
        if not player.playing:
            first = player.queue.get()
            await msg.delete()
            await player.play(first)
        else:
            await msg.edit(
                content=f"📋 **Плейлист добавлен:** {results.name} — `{len(results.tracks)} треков`",
                view=None
            )
        return

    # Если одиночный трек по ссылке (не поиск)
    if query.startswith("http") and len(results) == 1:
        track = results[0]
        await player.queue.put_wait(track)
        if not player.playing:
            await msg.delete()
            await player.play(player.queue.get())
        else:
            await msg.edit(
                content=f"➕ **Добавлено:** {track.title} `[{format_duration(track.length)}]`",
                view=None
            )
        return

    # Обычный поиск — показываем список
    tracks = results[:5]
    lines = ["**Результаты поиска:**\n"]
    for i, t in enumerate(tracks, 1):
        lines.append(f"`{i}.` {t.title} `[{format_duration(t.length)}]`")
    lines.append("\nВыбери трек кнопкой:")

    view = TrackSelectView(tracks, interaction.guild,
                           interaction.user.voice.channel,
                           interaction.channel, msg)
    await msg.edit(content="\n".join(lines), view=view)


@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and (player.playing or player.paused):
        await player.skip(force=True)
        await interaction.response.defer()
    else:
        await interaction.response.send_message("❗ Ничего не играет.")


@tree.command(name="skipto", description="Перейти к треку по номеру в очереди")
@app_commands.describe(position="Номер трека")
async def skipto_cmd(interaction: discord.Interaction, position: int):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(f"❗ Укажи номер от 1 до {len(q)}.")
        return
    for _ in range(position - 1):
        q.get()
    await player.skip(force=True)
    await interaction.response.send_message(f"⏩ Перехожу к треку #{position}.")


@tree.command(name="pause", description="Пауза / продолжить")
async def pause_cmd(interaction: discord.Interaction):
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
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        player.queue.clear()
        cancel_idle_timer(interaction.guild_id)  # Исправлен баг: чистим таймер
        await player.stop()
        await player.disconnect()
        await interaction.response.send_message("⏹ Остановлено.")
    else:
        await interaction.response.send_message("❗ Бот не в канале.")


@tree.command(name="volume", description="Громкость от 0 до 100")
@app_commands.describe(level="Уровень громкости (0–100)")
async def volume_cmd(interaction: discord.Interaction, level: int):
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
    player: wavelink.Player = interaction.guild.voice_client
    if player and len(player.queue) > 1:
        player.queue.shuffle()
        await interaction.response.send_message("🔀 Очередь перемешана.")
    else:
        await interaction.response.send_message("❗ Нечего перемешивать.")


@tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер трека в очереди")
async def remove_cmd(interaction: discord.Interaction, position: int):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❗ Бот не в канале.")
        return
    q = player.queue
    if position < 1 or position > len(q):
        await interaction.response.send_message(f"❗ Укажи номер от 1 до {len(q)}.")
        return
    track = q[position - 1]
    del q[position - 1]
    await interaction.response.send_message(f"🗑 Удалено: **{track.title}**")


@tree.command(name="queue", description="Показать очередь")
async def queue_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or (not player.current and player.queue.is_empty):
        await interaction.response.send_message("📭 Очередь пуста.")
        return

    lines = []
    if player.current:
        t = player.current
        link = f" — [открыть]({t.uri})" if t.uri else ""
        lines.append(f"🎵 **Сейчас:** {t.title} `[{format_duration(t.length)}]`{link}\n")

    if not player.queue.is_empty:
        lines.append("**В очереди:**")
        for i, t in enumerate(list(player.queue)[:10], 1):
            lines.append(f"`{i}.` 🎵 {t.title} `[{format_duration(t.length)}]`")
        if len(player.queue) > 10:
            lines.append(f"_...и ещё {len(player.queue) - 10} треков_")

    await interaction.response.send_message("\n".join(lines))


@tree.command(name="nowplaying", description="Что сейчас играет")
async def np_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        await interaction.response.send_message("📭 Ничего не играет.")
        return
    t = player.current
    link = f" — [открыть]({t.uri})" if t.uri else ""
    await interaction.response.send_message(
        f"🎵 **Сейчас играет:** {t.title} `[{format_duration(t.length)}]`{link}"
    )


# ─────────────────────────────────────────────
#  Автовыход из пустого канала
# ─────────────────────────────────────────────
@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
    if member.bot:
        return

    player: wavelink.Player = member.guild.voice_client
    if not player:
        return

    # Исправлен баг: кто-то зашёл обратно — отменяем таймер выхода
    if after.channel and after.channel == player.channel:
        cancel_idle_timer(member.guild.id)
        return

    non_bots = [m for m in player.channel.members if not m.bot]
    if len(non_bots) == 0:
        await asyncio.sleep(EMPTY_CH_TIMEOUT)
        if player.is_connected():
            non_bots = [m for m in player.channel.members if not m.bot]
            if len(non_bots) == 0:
                channel_id = getattr(player, "_text_channel_id", None)
                cancel_idle_timer(member.guild.id)
                await player.disconnect()
                if channel_id:
                    channel = member.guild.get_channel(channel_id)
                    if channel:
                        await channel.send("👋 Все ушли — выхожу из канала.")


# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Бот запущен как {bot.user}")
    nodes = [wavelink.Node(**n) for n in NODES]
    await wavelink.Pool.connect(nodes=nodes, client=bot)
    await tree.sync()
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="/play"
    ))


bot.run(DISCORD_TOKEN)
