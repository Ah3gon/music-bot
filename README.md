# 🎵 Discord Music Bot — YouTube + ВКонтакте

## Что умеет бот

| Команда | Описание |
|---|---|
| `/play yt <запрос>` | Найти и включить трек с YouTube |
| `/play vk <запрос>` | Найти и включить трек из ВКонтакте |
| `/skip` | Пропустить текущий трек |
| `/pause` | Пауза / продолжить |
| `/stop` | Остановить и отключиться |
| `/queue` | Показать очередь |
| `/nowplaying` | Что сейчас играет |

---

## Установка

### 1. Установи Python и зависимости

Нужен Python 3.10+. Скачай с [python.org](https://python.org).

Затем в папке с ботом выполни:
```
pip install -r requirements.txt
```

### 2. Установи FFmpeg

**Windows:**
- Скачай с https://ffmpeg.org/download.html
- Распакуй и добавь папку `bin` в PATH
- Или просто положи `ffmpeg.exe` рядом с `bot.py`

**Linux/Mac:**
```
sudo apt install ffmpeg     # Ubuntu/Debian
brew install ffmpeg          # macOS
```

---

## Настройка токенов

### Discord токен

1. Открой https://discord.com/developers/applications
2. Создай новое приложение → Bot → Reset Token
3. Включи: **Message Content Intent**, **Server Members Intent**
4. Скопируй токен и вставь в `bot.py` в строку `DISCORD_TOKEN = "..."`

Чтобы добавить бота на сервер:
- OAuth2 → URL Generator → галочки: `bot` + `applications.commands`
- Permissions: `Connect`, `Speak`, `Send Messages`, `Read Messages`
- Перейди по сгенерированной ссылке

---

### VK токен (для поиска по ВКонтакте)

VK закрыл официальный API аудио, поэтому используется библиотека `vkpymusic`.
Для получения токена нужно один раз выполнить:

```python
from vkpymusic import TokenReceiver

receiver = TokenReceiver("твой_логин_вк", "твой_пароль_вк")
if receiver.auth():
    receiver.save_to_config()
    print("Токен сохранён!")
```

Запусти этот скрипт один раз — он создаст файл конфига, который бот будет использовать автоматически.

> ⚠️ Используй отдельный VK-аккаунт, а не основной — на всякий случай.

---

## Запуск

```
python bot.py
```

Бот появится онлайн и зарегистрирует slash-команды (может занять до минуты).

---

## Где держать бота включённым 24/7?

Пока бот запущен только пока открыт терминал. Варианты для постоянной работы:

- **Бесплатно:** [Railway](https://railway.app) или [Render](https://render.com) — бесплатный план
- **Дёшево:** VPS за ~$3-5/месяц (Hetzner, DigitalOcean)
- **Для теста:** просто запускай на своём ПК когда нужно
