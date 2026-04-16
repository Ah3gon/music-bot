import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import random
import os          # ← добавь эту строку
import shutil
from collections import deque
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
IDLE_TIMEOUT     = 300   # секунд тишины перед автовыходом (5 минут)
EMPTY_CH_TIMEOUT = 60    # секунд в пустом канале перед выходом

# ─────────────────────────────────────────────
#  yt-dlp
# ─────────────────────────────────────────────

def _base_opts(extra: dict = {}) -> dict:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "cookiefile": "/app/cookies.txt",
    }
    opts.update(extra)
    return opts

YTDL_SEARCH_OPTS = _base_opts({"extract_flat": "in_playlist"})
YTDL_STREAM_OPTS = _base_opts()

# ─────────────────────────────────────────────
#  Бот
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ─────────────────────────────────────────────
#  Состояние сервера
# ─────────────────────────────────────────────
class GuildPlayer:
    def __init__(self):
        self.queue: deque[dict] = deque()
        self.current: Optional[dict] = None
        self.loop_mode: str = "off"      # "off" | "track" | "queue"
        self.volume: float = 1.0         # 0.0 – 2.0
        self.text_channel: Optional[discord.TextChannel] = None
        self.now_playing_msg: Optional[discord.Message] = None
        self.idle_task: Optional[asyncio.Task] = None
        # Предзагрузка следующего трека
        self.prefetch_url: Optional[str] = None
        self.prefetch_track: Optional[dict] = None
        self.prefetch_task: Optional[asyncio.Task] = None

    def reset(self):
        self.queue.clear()
        self.current = None
        self.loop_mode = "off"
        self.prefetch_url = None
        self.prefetch_track = None
        if self.prefetch_task:
            self.prefetch_task.cancel()
            self.prefetch_task = None
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None


players: dict[int, GuildPlayer] = {}

def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]


# ─────────────────────────────────────────────
#  Поиск и получение аудио
# ─────────────────────────────────────────────
async def search_youtube(query: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS) as ydl:
        info = await loop.run_in_executor(
            None, lambda: ydl.extract_info(f"ytsearch5:{query}", download=False)
        )
    results = []
    for entry in (info.get("entries") or [])[:5]:
        results.append({
            "title":    entry.get("title", "Без названия"),
            "url":      entry.get("url") or entry.get("webpage_url"),
            "webpage":  entry.get("webpage_url", ""),
            "duration": int(entry.get("duration") or 0),
        })
    return results


async def get_audio_url(track: dict) -> str:
    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL(YTDL_STREAM_OPTS) as ydl:
        info = await loop.run_in_executor(
            None, lambda: ydl.extract_info(track["url"], download=False)
        )
    return info["url"]


# ─────────────────────────────────────────────
#  Утилиты
# ─────────────────────────────────────────────
def format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ─────────────────────────────────────────────
#  Кнопки управления под "Сейчас играет"
# ─────────────────────────────────────────────
class PlayerControls(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = self.guild.voice_client
        if voice and voice.is_playing():
            voice.pause()
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
        elif voice and voice.is_paused():
            voice.resume()
            button.emoji = "⏸"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = self.guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
            await interaction.response.send_message("⏭ Пропущено.", ephemeral=True)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild.id)
        modes = ["off", "track", "queue"]
        player.loop_mode = modes[(modes.index(player.loop_mode) + 1) % 3]
        labels = {"off": "Повтор выкл ➡️", "track": "Повтор трека 🔂", "queue": "Повтор очереди 🔁"}
        await interaction.response.send_message(labels[player.loop_mode], ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild.id)
        if len(player.queue) > 1:
            lst = list(player.queue)
            random.shuffle(lst)
            player.queue = deque(lst)
            await interaction.response.send_message("🔀 Очередь перемешана.", ephemeral=True)
        else:
            await interaction.response.send_message("❗ Нечего перемешивать.", ephemeral=True)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = self.guild.voice_client
        player = get_player(self.guild.id)
        if voice:
            player.reset()
            voice.stop()
            await voice.disconnect()
        await interaction.response.send_message("⏹ Остановлено.", ephemeral=True)


# ─────────────────────────────────────────────
#  Воспроизведение
# ─────────────────────────────────────────────
async def send_now_playing(player: GuildPlayer, guild: discord.Guild):
    t = player.current
    if not t or not player.text_channel:
        return

    loop_labels = {"off": "выкл ➡️", "track": "трек 🔂", "queue": "очередь 🔁"}
    link = f" — [открыть]({t['webpage']})" if t.get("webpage") else ""
    text = (
        f"🎵 **Сейчас играет:** {t['title']} "
        f"`[{format_duration(t['duration'])}]`{link}\n"
        f"🔊 **{int(player.volume * 100)}%**  |  "
        f"Повтор: **{loop_labels[player.loop_mode]}**"
    )

    if player.now_playing_msg:
        try:
            await player.now_playing_msg.delete()
        except Exception:
            pass

    player.now_playing_msg = await player.text_channel.send(
        text, view=PlayerControls(guild)
    )


async def start_idle_timer(guild: discord.Guild, player: GuildPlayer):
    if player.idle_task:
        player.idle_task.cancel()

    async def _timer():
        await asyncio.sleep(IDLE_TIMEOUT)
        voice = guild.voice_client
        if voice and not voice.is_playing() and not voice.is_paused():
            player.reset()
            await voice.disconnect()
            if player.text_channel:
                await player.text_channel.send(
                    f"💤 Вышел — {IDLE_TIMEOUT // 60} мин тишины."
                )

    player.idle_task = asyncio.create_task(_timer())


async def prefetch_next(player: GuildPlayer):
    """Заранее получает аудио-URL следующего трека в фоне."""
    if not player.queue:
        return
    next_track = list(player.queue)[0]
    # Не перезагружаем если уже есть актуальный
    if player.prefetch_track == next_track and player.prefetch_url:
        return
    try:
        url = await get_audio_url(next_track)
        player.prefetch_url = url
        player.prefetch_track = next_track
    except Exception:
        player.prefetch_url = None
        player.prefetch_track = None


async def play_next(guild: discord.Guild, player: GuildPlayer, retry: int = 0):
    voice = guild.voice_client
    if not voice or not voice.is_connected():
        return

    if player.loop_mode == "track" and player.current:
        next_track = player.current
    elif player.loop_mode == "queue" and player.current:
        player.queue.append(player.current)
        next_track = player.queue.popleft() if player.queue else None
    else:
        next_track = player.queue.popleft() if player.queue else None

    if not next_track:
        player.current = None
        await start_idle_timer(guild, player)
        return

    player.current = next_track

    try:
        # Используем предзагруженный URL если он подходит
        if player.prefetch_track == next_track and player.prefetch_url:
            audio_url = player.prefetch_url
            player.prefetch_url = None
            player.prefetch_track = None
        else:
            audio_url = await get_audio_url(next_track)

        ffmpeg_opts = {
            "before_options": (
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                "-headers 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n'"
            ),
            "options": f"-vn -filter:a volume={player.volume}",
        }
        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)

        def after_play(error):
            if error:
                print(f"[!] Ошибка: {error}")
            asyncio.run_coroutine_threadsafe(
                play_next(guild, player), bot.loop
            )

        voice.play(source, after=after_play)
        await send_now_playing(player, guild)

        # Запускаем предзагрузку следующего трека в фоне
        if player.queue:
            if player.prefetch_task:
                player.prefetch_task.cancel()
            player.prefetch_task = asyncio.create_task(prefetch_next(player))

    except Exception as e:
        print(f"[!] Не удалось загрузить трек: {e}")
        if retry < 2:
            await asyncio.sleep(1)
            await play_next(guild, player, retry=retry + 1)
        else:
            if player.text_channel:
                await player.text_channel.send(
                    f"⚠️ Не удалось загрузить **{next_track['title']}**, пропускаю."
                )
            player.current = None
            await play_next(guild, player)


# ─────────────────────────────────────────────
#  View: выбор трека из результатов поиска
# ─────────────────────────────────────────────
class TrackSelectView(discord.ui.View):
    def __init__(self, results: list[dict], guild: discord.Guild,
                 voice_channel: discord.VoiceChannel,
                 text_channel: discord.TextChannel,
                 search_msg: discord.Message):
        super().__init__(timeout=60)
        self.results = results
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.search_msg = search_msg

        for i in range(len(results)):
            btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary)
            btn.callback = self._make_cb(i)
            self.add_item(btn)

        cancel = discord.ui.Button(label="✖ Отмена", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _make_cb(self, index: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            track = self.results[index]
            player = get_player(self.guild.id)
            player.text_channel = self.text_channel

            voice = self.guild.voice_client
            if voice is None:
                voice = await self.voice_channel.connect()
            elif voice.channel != self.voice_channel:
                await voice.move_to(self.voice_channel)

            player.queue.append(track)

            if not voice.is_playing() and not voice.is_paused():
                await self.search_msg.delete()
                await play_next(self.guild, player)
            else:
                if len(player.queue) == 1:
                    if player.prefetch_task:
                        player.prefetch_task.cancel()
                    player.prefetch_task = asyncio.create_task(prefetch_next(player))
                await self.search_msg.edit(
                    content=f"➕ **Добавлено:** {track['title']} `[{format_duration(track['duration'])}]`",
                    view=None
                )

            self.stop()
        return callback

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.search_msg.edit(content="❌ Отменено.", view=None)
        self.stop()


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

    # Отправляем сообщение о поиске
    msg = await interaction.followup.send(f"🔍 Ищу **{query}**...", wait=True)
    results = await search_youtube(query)

    if not results:
        await msg.edit(content="😕 Ничего не найдено.")
        return

    lines = ["**Результаты поиска:**\n"]
    for i, t in enumerate(results, 1):
        lines.append(f"`{i}.` {t['title']} `[{format_duration(t['duration'])}]`")
    lines.append("\nВыбери трек кнопкой:")

    view = TrackSelectView(results, interaction.guild,
                           interaction.user.voice.channel, interaction.channel, msg)
    await msg.edit(content="\n".join(lines), view=view)


@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and (voice.is_playing() or voice.is_paused()):
        voice.stop()
        await interaction.response.send_message("⏭ Пропущено.")
    else:
        await interaction.response.send_message("❗ Ничего не играет.")


@tree.command(name="skipto", description="Перейти к треку по номеру в очереди")
@app_commands.describe(position="Номер трека")
async def skipto_cmd(interaction: discord.Interaction, position: int):
    player = get_player(interaction.guild_id)
    if position < 1 or position > len(player.queue):
        await interaction.response.send_message(
            f"❗ Укажи номер от 1 до {len(player.queue)}."
        )
        return
    for _ in range(position - 1):
        player.queue.popleft()
    voice = interaction.guild.voice_client
    if voice:
        voice.stop()
    await interaction.response.send_message(f"⏩ Перехожу к треку #{position}.")


@tree.command(name="pause", description="Пауза / продолжить")
async def pause_cmd(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_playing():
        voice.pause()
        await interaction.response.send_message("⏸ Пауза.")
    elif voice and voice.is_paused():
        voice.resume()
        await interaction.response.send_message("▶️ Продолжаю.")
    else:
        await interaction.response.send_message("❗ Ничего не играет.")


@tree.command(name="stop", description="Остановить и очистить очередь")
async def stop_cmd(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    player = get_player(interaction.guild_id)
    if voice:
        player.reset()
        voice.stop()
        await voice.disconnect()
        await interaction.response.send_message("⏹ Остановлено.")
    else:
        await interaction.response.send_message("❗ Бот не в канале.")


@tree.command(name="volume", description="Громкость от 0 до 200")
@app_commands.describe(level="Уровень громкости (0–200)")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 200:
        await interaction.response.send_message("❗ Укажи число от 0 до 200.")
        return
    player = get_player(interaction.guild_id)
    player.volume = level / 100
    await interaction.response.send_message(f"🔊 Громкость: **{level}%**")


@tree.command(name="loop", description="Переключить режим повтора")
async def loop_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    modes = ["off", "track", "queue"]
    player.loop_mode = modes[(modes.index(player.loop_mode) + 1) % 3]
    labels = {"off": "выключен ➡️", "track": "повтор трека 🔂", "queue": "повтор очереди 🔁"}
    await interaction.response.send_message(f"Повтор: **{labels[player.loop_mode]}**")


@tree.command(name="shuffle", description="Перемешать очередь")
async def shuffle_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    if len(player.queue) > 1:
        lst = list(player.queue)
        random.shuffle(lst)
        player.queue = deque(lst)
        await interaction.response.send_message("🔀 Очередь перемешана.")
    else:
        await interaction.response.send_message("❗ Нечего перемешивать.")


@tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер трека в очереди")
async def remove_cmd(interaction: discord.Interaction, position: int):
    player = get_player(interaction.guild_id)
    if position < 1 or position > len(player.queue):
        await interaction.response.send_message(
            f"❗ Укажи номер от 1 до {len(player.queue)}."
        )
        return
    lst = list(player.queue)
    removed = lst.pop(position - 1)
    player.queue = deque(lst)
    await interaction.response.send_message(f"🗑 Удалено: **{removed['title']}**")


@tree.command(name="queue", description="Показать очередь")
async def queue_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    if not player.current and not player.queue:
        await interaction.response.send_message("📭 Очередь пуста.")
        return

    lines = []
    if player.current:
        t = player.current
        link = f" — [открыть]({t['webpage']})" if t.get("webpage") else ""
        lines.append(f"🎵 **Сейчас:** {t['title']} `[{format_duration(t['duration'])}]`{link}\n")

    if player.queue:
        lines.append("**В очереди:**")
        for i, t in enumerate(list(player.queue)[:10], 1):
            lines.append(f"`{i}.` 🎵 {t['title']} `[{format_duration(t['duration'])}]`")
        if len(player.queue) > 10:
            lines.append(f"_...и ещё {len(player.queue) - 10} треков_")

    await interaction.response.send_message("\n".join(lines))


@tree.command(name="nowplaying", description="Что сейчас играет")
async def np_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    if not player.current:
        await interaction.response.send_message("📭 Ничего не играет.")
        return
    t = player.current
    link = f" — [открыть]({t['webpage']})" if t.get("webpage") else ""
    await interaction.response.send_message(
        f"🎵 **Сейчас играет:** {t['title']} `[{format_duration(t['duration'])}]`{link}"
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
    voice = member.guild.voice_client
    if not voice:
        return
    non_bots = [m for m in voice.channel.members if not m.bot]
    if len(non_bots) == 0:
        await asyncio.sleep(EMPTY_CH_TIMEOUT)
        if voice.is_connected():
            non_bots = [m for m in voice.channel.members if not m.bot]
            if len(non_bots) == 0:
                player = get_player(member.guild.id)
                player.reset()
                voice.stop()
                await voice.disconnect()
                if player.text_channel:
                    await player.text_channel.send("👋 Все ушли — выхожу из канала.")


# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Бот запущен как {bot.user}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="/play"
    ))


bot.run(DISCORD_TOKEN)
