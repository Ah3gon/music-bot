"""Локализация Surge. Язык — настройка сервера (server_settings.language)."""
from core import log

DEFAULT_LANG = "ru"
LANGUAGES = ["ru", "en"]

_lang_cache: dict = {}


def set_cached_lang(guild_id, lang):
    """Запомнить язык сервера в кэше (вызывается из database.py)."""
    if guild_id:
        _lang_cache[guild_id] = lang if lang in LANGUAGES else DEFAULT_LANG


def get_lang(guild_id) -> str:
    return _lang_cache.get(guild_id, DEFAULT_LANG)


STRINGS = {
    # ─────────────── общие ошибки ───────────────
    "err.no_voice":        {"ru": "❗ Зайди в голосовой канал сначала.",
                            "en": "❗ Join a voice channel first."},
    "err.not_in_channel":  {"ru": "❗ Бот не в канале.",
                            "en": "❗ The bot is not in a voice channel."},
    "err.nothing_playing": {"ru": "❗ Ничего не играет.",
                            "en": "❗ Nothing is playing."},
    "err.no_db":           {"ru": "❗ База данных недоступна.",
                            "en": "❗ Database is unavailable."},
    "err.generic":         {"ru": "❗ Произошла ошибка. Попробуй ещё раз.",
                            "en": "❗ Something went wrong. Please try again."},
    "err.not_your_menu":   {"ru": "❗ Это не твоё меню.",
                            "en": "❗ This menu isn't yours."},
    "err.error":           {"ru": "❗ Ошибка: {error}",
                            "en": "❗ Error: {error}"},
    "err.bad_position":    {"ru": "❗ Укажи номер от 1 до {max}.",
                            "en": "❗ Pick a number from 1 to {max}."},
    "err.volume_range":    {"ru": "❗ Укажи число от 0 до 100.",
                            "en": "❗ Pick a number from 0 to 100."},
    "err.positive_seconds": {"ru": "❗ Число секунд должно быть положительным.",
                             "en": "❗ The number of seconds must be positive."},
    "err.no_seek_live":    {"ru": "❗ Нельзя перематывать прямой эфир.",
                            "en": "❗ Live streams can't be seeked."},
    "err.seek_max":        {"ru": "❗ Максимум {max} секунд за раз.",
                            "en": "❗ Maximum {max} seconds at a time."},
    "err.nothing_to_shuffle": {"ru": "❗ Нечего перемешивать.",
                               "en": "❗ Nothing to shuffle."},

    # ─────────────── воспроизведение ───────────────
    "play.searching":   {"ru": "🔍 Ищу **{query}**...",
                         "en": "🔍 Searching for **{query}**..."},
    "play.added":       {"ru": "➕ **Добавлено:** {title} `[{dur}]`",
                         "en": "➕ **Added:** {title} `[{dur}]`"},
    "play.results":     {"ru": "**Результаты поиска:**",
                         "en": "**Search results:**"},
    "play.pick_track":  {"ru": "Выбери трек кнопкой:",
                         "en": "Pick a track:"},

    # ─────────────── управление плеером ───────────────
    "ctl.skipped":   {"ru": "⏭ Пропущено.", "en": "⏭ Skipped."},
    "ctl.skipto":    {"ru": "⏩ Перехожу к треку #{position}.",
                      "en": "⏩ Jumping to track #{position}."},
    "ctl.seeked":    {"ru": "{arrow} Перемотано → `{pos}` / `{total}`",
                      "en": "{arrow} Seeked → `{pos}` / `{total}`"},
    "ctl.paused":    {"ru": "⏸ Пауза.", "en": "⏸ Paused."},
    "ctl.resumed":   {"ru": "▶️ Продолжаю.", "en": "▶️ Resuming."},
    "ctl.stopped":   {"ru": "⏹ Остановлено.", "en": "⏹ Stopped."},
    "ctl.shuffled":  {"ru": "🔀 Очередь перемешана.", "en": "🔀 Queue shuffled."},
    "ctl.volume":    {"ru": "🔊 Громкость: **{level}%**",
                      "en": "🔊 Volume: **{level}%**"},
    "ctl.removed":   {"ru": "🗑 Удалено: **{title}**", "en": "🗑 Removed: **{title}**"},
    "ctl.loop":      {"ru": "Повтор: **{mode}**", "en": "Loop: **{mode}**"},
    "loop.off":      {"ru": "выключен ➡️", "en": "off ➡️"},
    "loop.track":    {"ru": "повтор трека 🔂", "en": "current track 🔂"},
    "loop.queue":    {"ru": "повтор очереди 🔁", "en": "whole queue 🔁"},

    # ─────────────── очередь и история ───────────────
    "queue.empty":         {"ru": "📭 Очередь пуста.", "en": "📭 The queue is empty."},
    "queue.nothing":       {"ru": "📭 Ничего не играет.", "en": "📭 Nothing is playing."},
    "queue.history_empty": {"ru": "📭 История пуста.", "en": "📭 History is empty."},
    "queue.history_title": {"ru": "**История треков:**", "en": "**Track history:**"},
    "queue.open_link":     {"ru": "открыть", "en": "open"},

    # ─────────────── текст песни ───────────────
    "lyrics.header":    {"ru": "📝 **{title}** _(via {source}{synced})_",
                         "en": "📝 **{title}** _(via {source}{synced})_"},
    "lyrics.not_found": {"ru": "😕 Текст для **{title}** не найден.\n"
                               "_Проверено: {sources}._\n"
                               "_Если трек называется нестандартно, попробуй формат 'Артист - Название'_",
                         "en": "😕 No lyrics found for **{title}**.\n"
                               "_Checked: {sources}._\n"
                               "_If the track has an unusual name, try the 'Artist - Title' format_"},
    "lyrics.truncated": {"ru": "\n_...продолжение текста обрезано_",
                         "en": "\n_...lyrics truncated_"},

    # ─────────────── статистика ───────────────
    "stats.empty":       {"ru": "📭 Статистика пуста — включи первый трек!",
                          "en": "📭 No stats yet — play your first track!"},
    "stats.title":       {"ru": "📊 Статистика {bot}", "en": "📊 {bot} stats"},
    "stats.tracks":      {"ru": "🎵 Треков сыграно", "en": "🎵 Tracks played"},
    "stats.total_time":  {"ru": "⏱ Общее время", "en": "⏱ Total time"},
    "stats.duration":    {"ru": "**{h}ч {m}м**", "en": "**{h}h {m}m**"},
    "mystats.empty":     {"ru": "📭 У тебя пока нет статистики — включи пару треков!",
                          "en": "📭 You have no stats yet — play a few tracks!"},
    "mystats.title":     {"ru": "📊 Твоя статистика", "en": "📊 Your stats"},
    "mystats.listen_time": {"ru": "⏱ Время прослушивания", "en": "⏱ Listening time"},
    "mystats.top_artists": {"ru": "🎤 Любимые исполнители", "en": "🎤 Top artists"},

    # ─────────────── эффекты ───────────────
    "fx.off":     {"ru": "✅ Эффекты отключены.", "en": "✅ Effects turned off."},
    "fx.applied": {"ru": "✨ Эффект: **{name}**", "en": "✨ Effect: **{name}**"},
}


def t(guild_id, key: str, **kwargs) -> str:
    """Перевод по ключу с подстановкой: t(gid, 'ctl.volume', level=50)"""
    entry = STRINGS.get(key)
    if entry is None:
        log.warning("i18n: неизвестный ключ %r", key)
        return key
    text = entry.get(get_lang(guild_id)) or entry.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError) as e:
            log.warning("i18n: ошибка подстановки в %r: %s", key, e)
    return text
