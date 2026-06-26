import core
from core import bot, tree, DISCORD_TOKEN, log

# Импорт модулей регистрирует команды и события на bot/tree
import database
import spotify
import lyrics
import helpers
import playback
import views
import events
import commands_play
import commands_control
import commands_misc

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN не задан в .env")
    bot.run(DISCORD_TOKEN, log_handler=None)
