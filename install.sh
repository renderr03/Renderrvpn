#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_url="https://codeload.github.com/renderr03/Renderrvpn/tar.gz/refs/heads/main"
app=/opt/renderrvpn
config=/etc/renderrvpn
unit=/etc/systemd/system/renderrvpn.service

if (( EUID != 0 )); then
    printf '%s\n' 'Запусти установку от root или передай скрипт в sudo bash.' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null; then
    printf '%s\n' 'Для установщика нужен Linux VPS с systemd.' >&2
    exit 1
fi
if ! command -v flock >/dev/null; then
    printf '%s\n' 'Для установки нужен flock из пакета util-linux.' >&2
    exit 1
fi
exec 9>/run/lock/renderrvpn-install.lock
if ! flock -n 9; then
    printf '%s\n' 'Установка RenderrVPN уже выполняется.' >&2
    exit 1
fi

if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    if ! command -v apt-get >/dev/null; then
        printf '%s\n' 'Установи Python 3.10 или новее в /usr/bin/python3.' >&2
        exit 1
    fi
    apt-get update
    apt-get install -y python3 ca-certificates
    if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        printf '%s\n' 'На VPS нужен Python 3.10 или новее.' >&2
        exit 1
    fi
fi

install -d -m 0755 "$app" "$app/releases"
install -d -m 0700 "$config"
work=$(mktemp -d)
build_path=
previous=
next_link=
linked=0
finished=0
cleanup() {
    if (( ! finished && linked )); then
        if [[ -n "$previous" ]]; then
            ln -sfn "$previous" "$app/current"
            systemctl restart renderrvpn.service >/dev/null 2>&1 || true
        else
            rm -f "$app/current"
            systemctl disable --now renderrvpn.service >/dev/null 2>&1 || true
        fi
    fi
    if [[ -n "$next_link" ]]; then
        rm -f -- "$next_link"
    fi
    if (( ! finished )) && [[ -n "$build_path" ]]; then
        rm -rf -- "$build_path"
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT

printf '%s\n' 'Загружаю RenderrVPN из GitHub…'
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --retry 3 --max-time 90 "$repo_url" -o "$work/source.tar.gz"
build_path=$(mktemp -d "$app/releases/build.XXXXXXXX")
tar -xzf "$work/source.tar.gz" -C "$build_path" --strip-components=1 --no-same-owner --no-same-permissions
test -f "$build_path/renderrvpn/__main__.py"
chmod -R a+rX "$build_path"

if [[ ! -f "$config/bot.env" ]]; then
    if ! { exec 3<>/dev/tty; } 2>/dev/null; then
        printf '%s\n' 'Для первой установки запусти команду из интерактивного терминала.' >&2
        exit 1
    fi
    cd "$build_path"
    while true; do
        printf '%s' 'Токен Telegram-бота (ввод скрыт) > ' >&3
        IFS= read -rs telegram_token <&3 || exit 1
        printf '\n' >&3
        if [[ ! "$telegram_token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
            printf '%s\n' 'Введи токен, выданный BotFather.' >&3
            continue
        fi
        if TELEGRAM_BOT_TOKEN="$telegram_token" PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 - <<'PY'
import os
from renderrvpn.services import ServiceError, Telegram

bot = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
try:
    bot.call("getMe")
    if bot.call("getWebhookInfo").get("url"):
        print("У бота уже есть webhook. Создай отдельного бота в BotFather.")
        raise SystemExit(1)
except ServiceError as exc:
    print(f"Не удалось проверить токен. {exc}")
    raise SystemExit(1) from None
PY
        then
            break
        fi
    done
    while true; do
        printf '%s' 'Числовой Telegram ID администратора > ' >&3
        IFS= read -r admin_id <&3 || exit 1
        if [[ "$admin_id" =~ ^[1-9][0-9]{0,15}$ ]] && (( admin_id <= 4503599627370495 )); then
            break
        fi
        printf '%s\n' 'Введи числовой ID Telegram.' >&3
    done
    printf 'TELEGRAM_BOT_TOKEN=%s\nADMIN_IDS=%s\n' "$telegram_token" "$admin_id" > "$work/bot.env"
    install -m 0600 "$work/bot.env" "$config/bot.env"
    unset telegram_token
fi

cat > "$work/renderrvpn.service" <<'UNIT'
[Unit]
Description=RenderrVPN Telegram bot
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
WorkingDirectory=/opt/renderrvpn/current
EnvironmentFile=/etc/renderrvpn/bot.env
Environment=DATABASE_PATH=/var/lib/renderrvpn/renderrvpn.sqlite3
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/usr/bin/python3 -m renderrvpn --check-config
ExecStart=/usr/bin/python3 -m renderrvpn
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
DynamicUser=yes
StateDirectory=renderrvpn
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
UNIT

install -m 0644 "$work/renderrvpn.service" "$unit"
if [[ -L "$app/current" ]]; then
    previous=$(readlink "$app/current")
fi
next_link="$app/.next-${build_path##*/}"
ln -s "$build_path" "$next_link"
mv -Tf "$next_link" "$app/current"
linked=1
systemctl daemon-reload
systemctl enable renderrvpn.service
systemctl restart renderrvpn.service
systemctl is-active --quiet renderrvpn.service
finished=1
printf '%s\n' 'RenderrVPN установлен. Открой бота, нажми «Запустить», затем «Админка» → «Добавить основную панель 3x-ui».'
