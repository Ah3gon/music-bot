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

# Публичные Lavalink-ноды (используются по порядку, если одна упала)
NODES = [
    {"uri": "http://lavalink.jirayu.net:13592", "password": "youshallnotpass"},
    {"uri": "http://n3.nexcloud.in:2026",       "password": "nexcloud"},
    {"uri": "http://lava.g3v.co.uk:9008",       "password": "lavalinklol"},
]

# ─────────────────────────────────────────────
#  Бот
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# idle-таймеры на каждый сервер
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
        if player and not player.playing:
            await player.disconnect()
            await channel.send(f"💤 Вышел — {IDLE_TIMEOUT // 60} мин тишины.")

    idle_tasks[guild.id] = asyncio.create_task(_timer())


def cancel_idle_timer(guild_id: int):
    if guild_id in idle_tasks:
        idle_tasks[guild_id].cancel()
        del idle_tasks[guild_id]


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
            await interaction.response.send_message("⏭ Пропущено.", ephemeral=True)
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

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.player
        if p:
            p.queue.clear()
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

            # Подключаемся к голосовому каналу
            player: wavelink.Player = self.guild.voice_client
            if player is None:
                player = await self.voice_channel.connect(cls=wavelink.Player)
            elif player.channel != self.voice_channel:
                await player.move_to(self.voice_channel)

            player.autoplay = wavelink.AutoPlayMode.disabled

            if not player.playing:
                await self.search_msg.delete()
                await player.play(track)
                # Сообщение "Сейчас играет" появится через on_wavelink_track_start
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

    # Ищем текстовый канал (сохранён в player.extras)
    channel_id = getattr(player, "_text_channel_id", None)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not channel:
        return

    # Удаляем предыдущее "Сейчас играет"
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

    # Если очередь пуста — запускаем idle-таймер
    if player.queue.is_empty and not player.playing:
        if channel:
            await start_idle_timer(guild, channel)


@bot.event
async def on_wavelink_inactive_player(player: wavelink.Player):
    await player.disconnect()


# ─────────────────────────────────────────────
#  Slash-команды
# ─────────────────────────────────────────────
@tree.command(name="play", description="Поиск и воспроизведение трека с YouTube")
@app_commands.describe(query="Название трека или исполнитель")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        await interaction.followup.send("❗ Зайди в голосовой канал сначала.")
        return

    msg = await interaction.followup.send(f"🔍 Ищу **{query}**...", wait=True)

    # Поиск через Lavalink
    results = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)

    if not results:
        await msg.edit(content="😕 Ничего не найдено.")
        return

    tracks = results[:5]
    lines = ["**Результаты поиска:**\n"]
    for i, t in enumerate(tracks, 1):
        lines.append(f"`{i}.` {t.title} `[{format_duration(t.length)}]`")
    lines.append("\nВыбери трек кнопкой:")

    view = TrackSelectView(tracks, interaction.guild,
                           interaction.user.voice.channel,
                           interaction.channel, msg)

    # Запоминаем текстовый канал в плеере
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        player._text_channel_id = interaction.channel.id

    await msg.edit(content="\n".join(lines), view=view)


@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and (player.playing or player.paused):
        await player.skip(force=True)
        await interaction.response.send_message("⏭ Пропущено.")
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
    # Удаляем треки перед нужным
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
    non_bots = [m for m in player.channel.members if not m.bot]
    if len(non_bots) == 0:
        await asyncio.sleep(EMPTY_CH_TIMEOUT)
        if player.is_connected():
            non_bots = [m for m in player.channel.members if not m.bot]
            if len(non_bots) == 0:
                channel_id = getattr(player, "_text_channel_id", None)
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

    # Подключаем Lavalink-ноды
    nodes = [wavelink.Node(**n) for n in NODES]
    await wavelink.Pool.connect(nodes=nodes, client=bot)

    await tree.sync()
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="/play"
    ))


bot.run(DISCORD_TOKEN)
