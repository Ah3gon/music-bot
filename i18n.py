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
    # ─────────────── права и лимиты ───────────────
    "err.dj_required":  {"ru": "❗ Для этого нужна роль **DJ**.",
                         "en": "❗ You need the **DJ** role for that."},
    "err.track_limit":  {"ru": "❗ Ты достиг лимита треков (**{limit}** за сессию). "
                               "Дождись конца очереди или попроси DJ добавить больше.",
                         "en": "❗ You've hit the track limit (**{limit}** per session). "
                               "Wait for the queue to end or ask a DJ to raise it."},

    # ─────────────── карточка «сейчас играет» ───────────────
    "np.live":            {"ru": "📻 Прямой эфир", "en": "📻 Live stream"},
    "np.title":           {"ru": "🎵 Сейчас играет", "en": "🎵 Now playing"},
    "np.loop":            {"ru": "Повтор", "en": "Loop"},
    "np.volume":          {"ru": "Громкость", "en": "Volume"},
    "np.effect":          {"ru": "Эффект", "en": "Effect"},
    "np.next":            {"ru": "⏭ Следующий в очереди", "en": "⏭ Up next"},
    "np.birthday_footer": {"ru": "🎂 Поздравительный трек", "en": "🎂 Birthday track"},
    "np.idle_leave":      {"ru": "💤 Вышел — {min} мин тишины.",
                           "en": "💤 Left — {min} min of silence."},
    "loop.short_off":     {"ru": "выкл ➡️", "en": "off ➡️"},
    "loop.short_track":   {"ru": "трек 🔂", "en": "track 🔂"},
    "loop.short_queue":   {"ru": "очередь 🔁", "en": "queue 🔁"},

    # ─────────────── события ───────────────
    "evt.node_error":  {"ru": "⚠️ Lavalink-нода вернулась с ошибкой.\n"
                              "_Попробуй `/play <трек>` чтобы перезапустить плеер._",
                        "en": "⚠️ The Lavalink node returned an error.\n"
                              "_Try `/play <track>` to restart the player._"},
    "evt.all_left":    {"ru": "👋 Все ушли — выхожу из канала.",
                        "en": "👋 Everyone left — disconnecting."},
    "bday.greeting":   {"ru": "🎂 С Днём рождения, {mention}! 🎉 {bot} поздравляет тебя!",
                        "en": "🎂 Happy birthday, {mention}! 🎉 {bot} is celebrating with you!"},
    "bday.interrupt":  {"ru": "🎂 С Днём рождения, {mention}! 🎉 {bot} прерывает музыку ради поздравления!",
                        "en": "🎂 Happy birthday, {mention}! 🎉 {bot} is pausing the music to celebrate!"},

    # ─────────────── загрузка плейлистов ───────────────
    "err.voice_connect":  {"ru": "❗ Не удалось подключиться к голосовому каналу.",
                           "en": "❗ Couldn't connect to the voice channel."},
    "play.yt_fetching":   {"ru": "📋 Получаю список треков из YouTube-плейлиста...",
                           "en": "📋 Fetching the track list from the YouTube playlist..."},
    "play.yt_failed":     {"ru": "❗ Не удалось загрузить YouTube-плейлист.\n"
                                 "_Возможно, он приватный или удалён._",
                           "en": "❗ Couldn't load the YouTube playlist.\n"
                                 "_It may be private or deleted._"},
    "play.loading_n":     {"ru": "🔍 Загружаю {n} треков...",
                           "en": "🔍 Loading {n} tracks..."},
    "play.yt_none":       {"ru": "😕 Не удалось загрузить ни одного трека из плейлиста.",
                           "en": "😕 Couldn't load a single track from the playlist."},
    "play.yt_added":      {"ru": "📋 Добавлено из YT-плейлиста: `{n} треков`",
                           "en": "📋 Added from the YT playlist: `{n} tracks`"},
    "play.searching_yt_n": {"ru": "🔍 Ищу {n} треков на YouTube...",
                            "en": "🔍 Searching YouTube for {n} tracks..."},
    "play.yt_search_failed": {"ru": "😕 Не удалось найти треки на YouTube.",
                              "en": "😕 Couldn't find the tracks on YouTube."},
    "play.spotify_added": {"ru": "📋 Добавлено из Spotify: `{n} треков`",
                           "en": "📋 Added from Spotify: `{n} tracks`"},
    "play.nothing_found": {"ru": "😕 Ничего не найдено.", "en": "😕 Nothing found."},
    "play.limit_suffix":  {"ru": " (лимит {limit})", "en": " (limit {limit})"},
    "play.playlist_added": {"ru": "📋 **Плейлист добавлен:** {name} — `{count} треков`{suffix}",
                            "en": "📋 **Playlist added:** {name} — `{count} tracks`{suffix}"},

    # ─────────────── Яндекс / Spotify ───────────────
    "play.yandex_unsupported": {
        "ru": "❌ **Яндекс.Музыка не поддерживается.**\n"
              "_Причина: хостинг бота находится вне России, "
              "и Яндекс блокирует запросы к своему API._\n\n"
              "💡 **Что можно сделать:**\n"
              "• Найти этот же трек в Spotify — `/play <ссылка Spotify>`\n"
              "• Просто написать название трека — `/play Artist - Song`\n"
              "• Использовать ссылку на YouTube",
        "en": "❌ **Yandex Music is not supported.**\n"
              "_Reason: the bot is hosted outside Russia, "
              "and Yandex blocks requests to its API from abroad._\n\n"
              "💡 **What you can do:**\n"
              "• Find the same track on Spotify — `/play <Spotify link>`\n"
              "• Just type the track name — `/play Artist - Song`\n"
              "• Use a YouTube link"},
    "play.spotify_not_configured": {
        "ru": "❗ **Spotify не настроен на сервере бота.**\n"
              "_Администратор должен добавить SPOTIFY_CLIENT_ID "
              "и SPOTIFY_CLIENT_SECRET в переменные окружения._",
        "en": "❗ **Spotify is not configured on the bot's host.**\n"
              "_The admin needs to add SPOTIFY_CLIENT_ID "
              "and SPOTIFY_CLIENT_SECRET to the environment variables._"},
    "play.spotify_bad_url": {
        "ru": "❗ **Неверный формат ссылки Spotify.**\n"
              "_Поддерживаются: track, album, playlist._\n"
              "Пример: `https://open.spotify.com/playlist/...`",
        "en": "❗ **Invalid Spotify link format.**\n"
              "_Supported: track, album, playlist._\n"
              "Example: `https://open.spotify.com/playlist/...`"},
    "play.spotify_fetching": {"ru": "🎵 Получаю треки из Spotify через API...",
                              "en": "🎵 Fetching tracks from Spotify via the API..."},
    "sp.type_track":    {"ru": "трек", "en": "track"},
    "sp.type_album":    {"ru": "альбом", "en": "album"},
    "sp.type_playlist": {"ru": "плейлист", "en": "playlist"},
    "sp.type_resource": {"ru": "ресурс", "en": "resource"},
    "play.spotify_unavailable": {
        "ru": "❌ **{type_cap} недоступен через API.**\n\n"
              "Возможные причины:\n"
              "• {type_cap} **приватный** — Spotify API не отдаёт чужие приватные плейлисты\n"
              "• {type_cap} удалён или его не существует\n"
              "• Ссылка повреждена\n\n"
              "💡 **Как проверить:**\n"
              "1. Открой ссылку в режиме инкогнито (без логина)\n"
              "2. Если видишь страницу — публичный, что-то ещё не так\n"
              "3. Если просит залогиниться — приватный\n\n"
              "Чтобы сделать плейлист публичным: открой его в Spotify → "
              "три точки → «Сделать публичным»",
        "en": "❌ **This {type_low} is unavailable through the API.**\n\n"
              "Possible reasons:\n"
              "• The {type_low} is **private** — the Spotify API won't return other users' private playlists\n"
              "• The {type_low} was deleted or never existed\n"
              "• The link is broken\n\n"
              "💡 **How to check:**\n"
              "1. Open the link in incognito mode (logged out)\n"
              "2. If the page loads — it's public, something else is wrong\n"
              "3. If it asks you to log in — it's private\n\n"
              "To make a playlist public: open it in Spotify → "
              "three dots → «Make public»"},
    "play.spotify_bad_token": {
        "ru": "❌ **Spotify токен недействителен.**\n"
              "_Администратору: проверь SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET._",
        "en": "❌ **The Spotify token is invalid.**\n"
              "_Admin: check SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET._"},
    "play.spotify_api_down": {
        "ru": "❌ **Spotify API временно недоступен.**\n_Попробуй ещё раз через минуту._",
        "en": "❌ **The Spotify API is temporarily unavailable.**\n_Try again in a minute._"},
    "play.spotify_empty": {
        "ru": "❌ **{type_cap} пустой.**\n"
              "В нём нет треков, либо все треки недоступны в твоём регионе.",
        "en": "❌ **This {type_low} is empty.**\n"
              "It has no tracks, or none of them are available in your region."},
    "play.spotify_failed": {
        "ru": "❌ **Не удалось получить {type_low}.**\n_Ошибка: {error}_",
        "en": "❌ **Couldn't fetch the {type_low}.**\n_Error: {error}_"},
    "play.playlist_load_failed": {
        "ru": "⚠️ **Не получилось загрузить плейлист.**\n"
              "_Бесплатные Lavalink-ноды часто не справляются с YT-плейлистами._\n\n"
              "💡 Попробуй:\n"
              "• Скинуть ссылку на конкретное видео\n"
              "• Использовать Spotify-плейлист\n"
              "• Повторить через 1-2 минуты",
        "en": "⚠️ **Couldn't load the playlist.**\n"
              "_Free Lavalink nodes often choke on YouTube playlists._\n\n"
              "💡 Try:\n"
              "• Sending a link to a single video\n"
              "• Using a Spotify playlist\n"
              "• Retrying in 1-2 minutes"},
    "play.node_unavailable": {
        "ru": "⚠️ **Lavalink-нода временно недоступна.**\n"
              "_Это бывает с публичными нодами — YouTube периодически "
              "блокирует их или они уходят на обслуживание._\n\n"
              "💡 Попробуй:\n"
              "• Повторить через 30-60 секунд\n"
              "• Использовать прямую ссылку (YouTube/Spotify)\n"
              "• Если не работает 5+ минут — скинь админу логи",
        "en": "⚠️ **The Lavalink node is temporarily unavailable.**\n"
              "_This happens with public nodes — YouTube blocks them "
              "from time to time, or they go down for maintenance._\n\n"
              "💡 Try:\n"
              "• Retrying in 30-60 seconds\n"
              "• Using a direct link (YouTube/Spotify)\n"
              "• If it's broken for 5+ minutes, send the logs to your admin"},

    # ─────────────── savequeue ───────────────
    "err.name_empty":      {"ru": "❗ Название не может быть пустым.",
                            "en": "❗ The name can't be empty."},
    "err.name_too_long":   {"ru": "❗ Название должно быть не длиннее {max} символов.",
                            "en": "❗ The name must be at most {max} characters."},
    "err.queue_empty":     {"ru": "❗ Очередь пуста.", "en": "❗ The queue is empty."},
    "err.playlist_exists": {"ru": "❗ Плейлист **{name}** уже существует.",
                            "en": "❗ Playlist **{name}** already exists."},
    "savequeue.done":      {"ru": "✅ Очередь сохранена как **{name}** — `{n} треков`!",
                            "en": "✅ Queue saved as **{name}** — `{n} tracks`!"},

    # ─────────────── интерфейс (views) ───────────────
    "addpl.added": {"ru": "✅ **{p0}** добавлен в плейлист!",
                   "en": "✅ **{p0}** added to the playlist!"},
    "addpl.added_named": {"ru": "✅ **{p0}** добавлен в **{p1}**!",
                         "en": "✅ **{p0}** added to **{p1}**!"},
    "addpl.count": {"ru": "{p0} треков",
                   "en": "{p0} tracks"},
    "addpl.create_failed": {"ru": "❗ Не удалось создать плейлист.",
                           "en": "❗ Couldn't create the playlist."},
    "addpl.create_new": {"ru": "Создать новый плейлист",
                        "en": "Create a new playlist"},
    "addpl.modal_title": {"ru": "Добавить в плейлист",
                         "en": "Add to playlist"},
    "addpl.name_label": {"ru": "Название плейлиста",
                        "en": "Playlist name"},
    "addpl.name_ph": {"ru": "Введи название плейлиста",
                     "en": "Enter a playlist name"},
    "addpl.pick": {"ru": "Выбери плейлист…",
                  "en": "Pick a playlist…"},
    "btn.dj_required": {"ru": "❗ Нужна роль DJ.",
                       "en": "❗ You need the DJ role."},
    "btn.error": {"ru": "❗ Ошибка кнопки. Попробуй позже.",
                 "en": "❗ Button error. Try again later."},
    "btn.loop": {"ru": "Повтор",
                "en": "Loop"},
    "btn.loop_off": {"ru": "Повтор выкл ➡️",
                    "en": "Loop off ➡️"},
    "btn.loop_queue": {"ru": "Повтор очереди 🔁",
                      "en": "Loop queue 🔁"},
    "btn.loop_track": {"ru": "Повтор трека 🔂",
                      "en": "Loop track 🔂"},
    "btn.pause": {"ru": "Пауза",
                 "en": "Pause"},
    "btn.pick_playlist": {"ru": "💾 Выбери плейлист:",
                         "en": "💾 Pick a playlist:"},
    "btn.play": {"ru": "Играть",
                "en": "Play"},
    "btn.queue": {"ru": "Очередь",
                 "en": "Queue"},
    "btn.skip": {"ru": "Скип",
                "en": "Skip"},
    "btn.stop": {"ru": "Стоп",
                "en": "Stop"},
    "btn.to_playlist": {"ru": "В плейлист",
                       "en": "To playlist"},
    "btn.vol_down": {"ru": "🔉 Громкость: **{p0}%**",
                    "en": "🔉 Volume: **{p0}%**"},
    "btn.vol_up": {"ru": "🔊 Громкость: **{p0}%**",
                  "en": "🔊 Volume: **{p0}%**"},
    "edit.no_versions": {"ru": "😕 Не нашёл вариантов для замены.",
                        "en": "😕 No replacement versions found."},
    "edit.not_yours": {"ru": "❗ Это не твой редактор.",
                      "en": "❗ This editor isn't yours."},
    "edit.now_empty": {"ru": "📭 Плейлист теперь пуст.",
                      "en": "📭 The playlist is now empty."},
    "edit.pick_version": {"ru": "🔄 Выбери версию для **{p0}**:",
                         "en": "🔄 Pick a version for **{p0}**:"},
    "edit.version_updated": {"ru": "\n\n✅ Версия обновлена: **{p0}**",
                            "en": "\n\n✅ Version updated: **{p0}**"},
    "jump.label": {"ru": "Номер страницы",
                  "en": "Page number"},
    "jump.need_number": {"ru": "❗ Введи число.",
                        "en": "❗ Enter a number."},
    "jump.placeholder": {"ru": "Введи число",
                        "en": "Enter a number"},
    "jump.range": {"ru": "❗ Номер от 1 до {p0}.",
                  "en": "❗ Pick a number from 1 to {p0}."},
    "jump.title": {"ru": "Перейти к странице",
                  "en": "Go to page"},
    "panel.cancel": {"ru": "↩️ Отмена",
                    "en": "↩️ Cancel"},
    "panel.change": {"ru": "Изменить настройку…",
                    "en": "Change a setting…"},
    "panel.collapse": {"ru": "⚙️ Свернуть",
                      "en": "⚙️ Collapse"},
    "panel.desc": {"ru": "Меняй настройки через меню и кнопки ниже.",
                  "en": "Change settings with the menu and buttons below."},
    "panel.expand": {"ru": "⚙️ Все настройки",
                    "en": "⚙️ All settings"},
    "panel.f_announce": {"ru": "📢 Объявлять «сейчас играет»",
                        "en": "📢 Announce \"now playing\""},
    "panel.f_dj": {"ru": "🎧 DJ-роль",
                  "en": "🎧 DJ role"},
    "panel.f_empty": {"ru": "🚪 Таймаут пустого канала",
                     "en": "🚪 Empty channel timeout"},
    "panel.f_fair": {"ru": "⚖️ Справедливая очередь",
                    "en": "⚖️ Fair queue"},
    "panel.f_idle": {"ru": "⏲ Таймаут бездействия",
                    "en": "⏲ Idle timeout"},
    "panel.f_limit": {"ru": "📏 Лимит треков на человека",
                     "en": "📏 Track limit per person"},
    "panel.f_source": {"ru": "🔍 Источник поиска",
                      "en": "🔍 Search source"},
    "panel.f_volume": {"ru": "🔊 Громкость по умолчанию",
                      "en": "🔊 Default volume"},
    "panel.f_voteskip": {"ru": "⏭ Голосование за скип",
                        "en": "⏭ Skip voting"},
    "panel.footer": {"ru": "Доступно админам сервера",
                    "en": "Available to server admins"},
    "panel.need_int": {"ru": "❗ Нужно целое число.",
                      "en": "❗ A whole number is required."},
    "panel.need_perm": {"ru": "❗ Нужно право «Управление сервером».",
                       "en": "❗ You need the \"Manage Server\" permission."},
    "panel.no": {"ru": "нет",
                "en": "no"},
    "panel.no_limit": {"ru": "без лимита",
                      "en": "no limit"},
    "panel.not_set": {"ru": "не задана",
                     "en": "not set"},
    "panel.now_off": {"ru": "Сейчас: выключено",
                     "en": "Now: disabled"},
    "panel.now_on": {"ru": "Сейчас: включено",
                    "en": "Now: enabled"},
    "panel.now_value": {"ru": "Сейчас: {p0}",
                       "en": "Now: {p0}"},
    "panel.number_range": {"ru": "Число от {p0} до {p1}",
                          "en": "A number from {p0} to {p1}"},
    "panel.off": {"ru": "выключено",
                 "en": "disabled"},
    "panel.off_f": {"ru": "выключена",
                   "en": "disabled"},
    "panel.on_f": {"ru": "включена",
                  "en": "enabled"},
    "panel.on_threshold": {"ru": "включено · порог {p0}%",
                          "en": "enabled · threshold {p0}%"},
    "panel.pick_dj": {"ru": "🎧 Выбрать DJ-роль…",
                     "en": "🎧 Pick a DJ role…"},
    "panel.reset_all": {"ru": "♻️ Сбросить всё",
                       "en": "♻️ Reset everything"},
    "panel.reset_confirm": {"ru": "♻️ Сбросить все настройки?",
                           "en": "♻️ Reset all settings?"},
    "panel.reset_desc": {"ru": "Все настройки сервера вернутся к значениям по умолчанию.\nЭто действие нельзя отменить.",
                        "en": "All server settings will return to their defaults.\nThis cannot be undone."},
    "panel.reset_dj": {"ru": "Сбросить DJ-роль",
                      "en": "Reset DJ role"},
    "panel.reset_yes": {"ru": "✅ Да, сбросить",
                       "en": "✅ Yes, reset"},
    "panel.seconds": {"ru": "{p0} сек",
                     "en": "{p0} sec"},
    "panel.title": {"ru": "⚙️ Настройки сервера",
                   "en": "⚙️ Server settings"},
    "panel.yandex": {"ru": "Яндекс",
                    "en": "Yandex"},
    "panel.yes": {"ru": "да",
                 "en": "yes"},
    "queue.compact": {"ru": "📑 Компактно",
                     "en": "📑 Compact"},
    "queue.detailed": {"ru": "📋 Подробно",
                      "en": "📋 Detailed"},
    "queue.footer": {"ru": "{p0} треков · страница {p1}/{p2}",
                    "en": "{p0} tracks · page {p1}/{p2}"},
    "queue.jump_btn": {"ru": "🔢 К странице",
                      "en": "🔢 Go to page"},
    "queue.now": {"ru": "🎵 Сейчас",
                 "en": "🎵 Now"},
    "queue.open": {"ru": "— [открыть]({p0})",
                  "en": " — [open]({p0})"},
    "queue.title": {"ru": "📜 Очередь",
                   "en": "📜 Queue"},
    "sel.cancel": {"ru": "✖ Отмена",
                  "en": "✖ Cancel"},
    "sel.cancelled": {"ru": "❌ Отменено.",
                     "en": "❌ Cancelled."},
    "sel.connect_error": {"ru": "❗ Не удалось подключиться: {p0}",
                         "en": "❗ Couldn't connect: {p0}"},
    "sel.connect_failed": {"ru": "❗ Не удалось подключиться к голосовому каналу.\n_Lavalink-ноды могут быть недоступны или Discord не отвечает. Попробуй ещё раз через минуту._",
                          "en": "❗ Couldn't connect to the voice channel.\n_Lavalink nodes may be down or Discord isn't responding. Try again in a minute._"},
    "sel.not_yours": {"ru": "❗ Это меню выбора не для тебя.",
                     "en": "❗ This selection menu isn't yours."},
    "sel.timeout": {"ru": "⏱ Время вышло.",
                   "en": "⏱ Timed out."},
    "vote.failed": {"ru": "❌ Голосование завершилось — недостаточно голосов ({p0} за, нужно было больше).",
                   "en": "❌ Vote ended — not enough votes ({p0} in favour, more were needed)."},
    "vote.join_first": {"ru": "❗ Войди в голосовой канал чтобы голосовать.",
                       "en": "❗ Join the voice channel to vote."},
    "vote.no_btn": {"ru": "❌ Против",
                   "en": "❌ Keep it"},
    "vote.passed": {"ru": "✅ Голосование прошло — трек пропущен!",
                   "en": "✅ Vote passed — track skipped!"},
    "vote.prompt": {"ru": "⏭ **Голосование за скип**\n✅ За: **{p0}** | ❌ Против: **{p1}**\nНужно **{p2}** из **{p3}** голосов | Осталось 30 сек",
                   "en": "⏭ **Skip vote**\n✅ Yes: **{p0}** | ❌ No: **{p1}**\nNeed **{p2}** of **{p3}** votes | 30 sec left"},
    "vote.yes_btn": {"ru": "✅ За скип",
                    "en": "✅ Skip it"},

    # ─────────────── редактор плейлиста и панель ───────────────
    "edit.pick_track":     {"ru": "Выбери трек…", "en": "Pick a track…"},
    "edit.delete":         {"ru": "🗑 Удалить", "en": "🗑 Delete"},
    "edit.change_version": {"ru": "🔄 Сменить версию", "en": "🔄 Change version"},
    "edit.header":         {"ru": "✏️ **Редактор «{p0}»** — {p1} треков",
                            "en": "✏️ **Editing «{p0}»** — {p1} tracks"},
    "edit.page":           {"ru": "  ·  стр. {p0}/{p1}", "en": "  ·  page {p0}/{p1}"},
    "edit.selected":       {"ru": "\n\nВыбран: **{p0}**", "en": "\n\nSelected: **{p0}**"},
    "edit.closed":         {"ru": "⏱ Редактор закрыт.", "en": "⏱ Editor closed."},
    "ver.pick":            {"ru": "Выбери нужную версию…", "en": "Pick the right version…"},
    "ver.cancel":          {"ru": "Отмена", "en": "Cancel"},
    "panel.back":          {"ru": "↩️ Назад", "en": "↩️ Back"},
    "panel.pick_value":    {"ru": "Выбери значение в меню ниже.",
                            "en": "Pick a value from the menu below."},
    "panel.o_voteskip":    {"ru": "Голосование за скип", "en": "Skip voting"},
    "panel.o_fair":        {"ru": "Справедливая очередь", "en": "Fair queue"},
    "panel.o_announce":    {"ru": "Объявлять «сейчас играет»", "en": "Announce \"now playing\""},
    "panel.o_volume":      {"ru": "Громкость по умолчанию (%)", "en": "Default volume (%)"},
    "panel.o_threshold":   {"ru": "Порог голосования (%)", "en": "Vote threshold (%)"},
    "panel.o_limit":       {"ru": "Лимит треков (0 = без лимита)",
                            "en": "Track limit (0 = unlimited)"},
    "panel.o_idle":        {"ru": "Таймаут бездействия (сек)", "en": "Idle timeout (sec)"},
    "panel.o_empty":       {"ru": "Таймаут пустого канала (сек)",
                            "en": "Empty channel timeout (sec)"},
    "panel.o_source":      {"ru": "Источник поиска по умолчанию", "en": "Default search source"},
    "panel.o_language":    {"ru": "Язык бота / Bot language", "en": "Bot language / Язык бота"},

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
