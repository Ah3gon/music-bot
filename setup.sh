#!/usr/bin/env bash
# ============================================================
#  Surge Discord Music Bot — установка с нуля (Ubuntu 24/26)
#  Ставит: Java + Lavalink (+плагины), PostgreSQL, бота (venv),
#  systemd-сервисы, еженедельный рестарт, ежедневные бэкапы БД.
#  Секреты вводятся интерактивно и НЕ хранятся в этом файле.
# ============================================================
set -euo pipefail

REPO_URL="https://github.com/Ah3gon/music-bot.git"
BOT_DIR="/opt/bot"
LAVA_DIR="/opt/lavalink"
BACKUP_DIR="/opt/backups"

say()  { echo -e "\n\033[1;35m==> $*\033[0m"; }
ok()   { echo -e "\033[1;32m✓ $*\033[0m"; }

[ "$(id -u)" = "0" ] || { echo "Запусти от root: bash setup.sh"; exit 1; }

# ---------- 0. Секреты (вводим в начале, чтобы потом не ждать) ----------
say "Ввод секретов (вставляй/вводи значение и жми Enter)"
read -rp "Discord bot token: " DISCORD_TOKEN
read -rp "Spotify Client ID: " SPOTIFY_CLIENT_ID
read -rp "Spotify Client Secret: " SPOTIFY_CLIENT_SECRET
read -rp "Spotify Refresh Token: " SPOTIFY_REFRESH_TOKEN
read -rp "Genius token: " GENIUS_TOKEN
read -rp "YouTube refresh token (пусто = пройти OAuth заново): " YT_REFRESH

DB_PASS=$(openssl rand -hex 16)
LAVA_PASS=$(openssl rand -hex 16)

# ---------- 1. Пакеты ----------
say "Установка пакетов (Java, PostgreSQL, Python, git)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y openjdk-21-jre-headless || apt-get install -y default-jre-headless
apt-get install -y postgresql postgresql-contrib python3-venv python3-pip git curl wget unzip openssl
systemctl enable --now postgresql
ok "Пакеты установлены"

# ---------- 2. PostgreSQL ----------
say "Настройка PostgreSQL (пользователь и база surge)"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='surge'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER surge WITH PASSWORD '${DB_PASS}';"
sudo -u postgres psql -c "ALTER USER surge WITH PASSWORD '${DB_PASS}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='surge'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE surge OWNER surge;"
ok "PostgreSQL готов"

# ---------- 3. Lavalink ----------
say "Установка Lavalink"
mkdir -p "$LAVA_DIR"
cd "$LAVA_DIR"
wget -q --show-progress -O Lavalink.jar \
  "https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar"

if [ -n "$YT_REFRESH" ]; then
  YT_OAUTH_BLOCK="    oauth:
      enabled: true
      refreshToken: \"${YT_REFRESH}\"
      skipInitialization: true"
else
  YT_OAUTH_BLOCK="    oauth:
      enabled: true"
fi

cat > application.yml << YML
server:
  port: 2333
  address: 127.0.0.1

lavalink:
  plugins:
    - dependency: "dev.lavalink.youtube:youtube-plugin:1.18.1"
      repository: "https://maven.lavalink.dev/releases"
    - dependency: "com.github.topi314.lavasrc:lavasrc-plugin:4.1.1"
      repository: "https://maven.lavalink.dev/releases"
  server:
    password: "${LAVA_PASS}"
    sources:
      youtube: false
      soundcloud: true
      http: true

plugins:
  youtube:
    enabled: true
    allowSearch: true
    clients:
      - MUSIC
      - WEB
      - WEB_REMIX
      - ANDROID_VR
      - TVHTML5EMBEDDED
${YT_OAUTH_BLOCK}
  lavasrc:
    sources:
      yandexmusic: false
    # Яндекс включим отдельным шагом (нужен токен + рабочий прокси):
    # yandexmusic:
    #   accessToken: "ТОКЕН"
    #   playlistLoadLimit: 10

logging:
  level:
    root: INFO
YML
chmod 600 application.yml

cat > /etc/systemd/system/lavalink.service << 'UNIT'
[Unit]
Description=Lavalink audio node
After=network.target

[Service]
WorkingDirectory=/opt/lavalink
ExecStart=/usr/bin/java -Xmx1G -jar Lavalink.jar
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
ok "Lavalink настроен"

# ---------- 4. Бот ----------
say "Установка бота из GitHub"
if [ -d "$BOT_DIR/.git" ]; then
  cd "$BOT_DIR" && git pull
else
  git clone "$REPO_URL" "$BOT_DIR"
fi
cd "$BOT_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

cat > .env << ENV
DISCORD_TOKEN=${DISCORD_TOKEN}
DATABASE_URL=postgresql://surge:${DB_PASS}@127.0.0.1:5432/surge
LAVALINK_URI=http://127.0.0.1:2333
LAVALINK_PASSWORD=${LAVA_PASS}
SPOTIFY_CLIENT_ID=${SPOTIFY_CLIENT_ID}
SPOTIFY_CLIENT_SECRET=${SPOTIFY_CLIENT_SECRET}
SPOTIFY_REFRESH_TOKEN=${SPOTIFY_REFRESH_TOKEN}
GENIUS_TOKEN=${GENIUS_TOKEN}
ENV
chmod 600 .env

cat > /etc/systemd/system/surge-bot.service << 'UNIT'
[Unit]
Description=Surge Discord Music Bot
After=network.target postgresql.service lavalink.service
Wants=lavalink.service

[Service]
WorkingDirectory=/opt/bot
ExecStartPre=/bin/sleep 20
ExecStart=/opt/bot/venv/bin/python /opt/bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
ok "Бот установлен (.env создан, права 600)"

# ---------- 5. Еженедельный рестарт (Пн 05:00 UTC) ----------
say "Еженедельный профилактический рестарт"
cat > /etc/systemd/system/surge-weekly-restart.service << 'UNIT'
[Unit]
Description=Weekly restart of Surge bot

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart surge-bot
UNIT
cat > /etc/systemd/system/surge-weekly-restart.timer << 'UNIT'
[Unit]
Description=Weekly restart timer for Surge bot

[Timer]
OnCalendar=Mon *-*-* 05:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNIT
ok "Таймер рестарта создан"

# ---------- 6. Ежедневные бэкапы БД (04:30, хранить 14 шт.) ----------
say "Ежедневные бэкапы базы"
mkdir -p "$BACKUP_DIR"
chown postgres:postgres "$BACKUP_DIR"
cat > "$BACKUP_DIR/backup.sh" << 'BK'
#!/usr/bin/env bash
set -e
STAMP=$(date +%Y-%m-%d_%H%M)
pg_dump surge | gzip > /opt/backups/surge_${STAMP}.sql.gz
find /opt/backups -name "surge_*.sql.gz" -mtime +14 -delete
BK
chmod +x "$BACKUP_DIR/backup.sh"
cat > /etc/systemd/system/surge-backup.service << 'UNIT'
[Unit]
Description=Daily Surge DB backup

[Service]
Type=oneshot
User=postgres
ExecStart=/opt/backups/backup.sh
UNIT
cat > /etc/systemd/system/surge-backup.timer << 'UNIT'
[Unit]
Description=Daily Surge DB backup timer

[Timer]
OnCalendar=*-*-* 04:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNIT
ok "Бэкапы настроены (локально, 14 дней; выгрузку в GitHub добавим отдельно)"

# ---------- 7. Запуск ----------
say "Запуск сервисов"
systemctl daemon-reload
systemctl enable lavalink surge-bot surge-weekly-restart.timer surge-backup.timer
systemctl restart lavalink
echo "Жду 25 сек, пока Lavalink поднимется и скачает плагины..."
sleep 25
systemctl restart surge-bot
sleep 12

echo ""
echo "============================================================"
systemctl is-active postgresql lavalink surge-bot | paste <(echo -e "postgresql\nlavalink\nsurge-bot") -
echo "============================================================"
echo "Пароль БД и Lavalink сгенерированы и уже прописаны в конфиги."
echo "Логи бота:      journalctl -u surge-bot -n 30 --no-pager"
echo "Логи Lavalink:  journalctl -u lavalink -n 30 --no-pager"
if [ -z "$YT_REFRESH" ]; then
  echo ""
  echo "⚠ YouTube OAuth: токен не вводился. Смотри логи Lavalink —"
  echo "  там появится ссылка google.com/device и код для привязки"
  echo "  бернер-аккаунта. После привязки токен появится в логах."
fi
echo "Готово! 🎉"
