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
    # ── общие ошибки ──
    "err.no_voice":     {"ru": "❗ Зайди в голосовой канал сначала.",
                         "en": "❗ Join a voice channel first."},
    "err.not_in_channel": {"ru": "❗ Бот не в канале.",
                           "en": "❗ The bot is not in a voice channel."},
    "err.nothing_playing": {"ru": "❗ Ничего не играет.",
                            "en": "❗ Nothing is playing."},
    "err.no_db":        {"ru": "❗ База данных недоступна.",
                         "en": "❗ Database is unavailable."},
    "err.generic":      {"ru": "❗ Произошла ошибка. Попробуй ещё раз.",
                         "en": "❗ Something went wrong. Please try again."},
    "err.not_your_menu": {"ru": "❗ Это не твоё меню.",
                          "en": "❗ This menu isn't yours."},
    # ── воспроизведение ──
    "play.searching":   {"ru": "🔍 Ищу **{query}**...",
                         "en": "🔍 Searching for **{query}**..."},
    "play.added":       {"ru": "➕ **Добавлено:** {title} `[{dur}]`",
                         "en": "➕ **Added:** {title} `[{dur}]`"},
    "play.results":     {"ru": "**Результаты поиска:**",
                         "en": "**Search results:**"},
    "play.pick_track":  {"ru": "Выбери трек кнопкой:",
                         "en": "Pick a track:"},
    # ── управление ──
    "ctl.skipped":      {"ru": "⏭ Пропущено.", "en": "⏭ Skipped."},
    "ctl.paused":       {"ru": "⏸ Пауза.", "en": "⏸ Paused."},
    "ctl.resumed":      {"ru": "▶️ Продолжаю.", "en": "▶️ Resuming."},
    "ctl.stopped":      {"ru": "⏹ Остановлено.", "en": "⏹ Stopped."},
    "ctl.shuffled":     {"ru": "🔀 Очередь перемешана.",
                         "en": "🔀 Queue shuffled."},
    "ctl.volume":       {"ru": "🔊 Громкость: **{level}%**",
                         "en": "🔊 Volume: **{level}%**"},
    "ctl.removed":      {"ru": "🗑 Удалено: **{title}**",
                         "en": "🗑 Removed: **{title}**"},
    # ── очередь ──
    "queue.empty":      {"ru": "📭 Очередь пуста.", "en": "📭 The queue is empty."},
    "queue.history_empty": {"ru": "📭 История пуста.",
                            "en": "📭 History is empty."},
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
