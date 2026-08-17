#!/usr/bin/env bash
#
# randomize_paths.sh — прячет дашборд и префикс подписки Marzban за случайными
# путями вместо дефолтных /dashboard/ и /sub/.
#
# Зачем: дефолтные пути — прямая мишень для сканеров ("это Marzban? проверим
# /dashboard/") и, что важнее для DPI в РФ, узнаваемый паттерн самих ссылок
# подписки — даже с уникальным токеном клиента префикс /sub/ выдаёт "это
# VPN-панель" любому наблюдателю на пути. Токен в подписке и так случайный,
# но путь-обёртка вокруг него — нет, пока не прогонишь этот скрипт.
#
# НЕ трогает саму ноду — там путей нет вообще (голый TLS REALITY на 443),
# нода уже защищена mTLS-сертификатом + IP-скоупом UFW, это сильнее.
#
# Запуск (на мастере, под root, рядом с /opt/marzban/.env):
#   bash panel/randomize_paths.sh
#
# Идемпотентно можно гонять повторно — перегенерирует оба пути заново
# (например, если подозреваешь, что старый путь мог засветиться).

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; NC='\033[0m'
ok(){ echo -e "${G}✓${NC} $*"; }; warn(){ echo -e "${Y}⚠${NC}  $*"; }

MARZBAN_DIR="${MARZBAN_DIR:-/opt/marzban}"
ENV_FILE="$MARZBAN_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "Не нашёл $ENV_FILE (задай MARZBAN_DIR=...)"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Запускай под root."; exit 1; }

rand_hex(){ python3 -c "import secrets; print(secrets.token_hex(8))"; }

NEW_DASHBOARD="/$(rand_hex)/"
NEW_SUB="$(rand_hex)"

_set_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}[[:space:]]*=" "$ENV_FILE"; then
    sed -i.bak -E "s|^${key}[[:space:]]*=.*|${key} = \"${val}\"|" "$ENV_FILE"
  elif grep -qE "^#[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE"; then
    sed -i.bak -E "s|^#[[:space:]]*${key}[[:space:]]*=.*|${key} = \"${val}\"|" "$ENV_FILE"
  else
    printf '%s = "%s"\n' "$key" "$val" >> "$ENV_FILE"
  fi
  rm -f "$ENV_FILE.bak"
}

OLD_DASHBOARD="$(grep -E '^DASHBOARD_PATH' "$ENV_FILE" | sed -E 's/.*=\s*"?([^"]*)"?/\1/' || true)"
OLD_SUB="$(grep -E '^XRAY_SUBSCRIPTION_PATH' "$ENV_FILE" | sed -E 's/.*=\s*"?([^"]*)"?/\1/' || true)"

_set_env "DASHBOARD_PATH" "$NEW_DASHBOARD"
_set_env "XRAY_SUBSCRIPTION_PATH" "$NEW_SUB"

ok "DASHBOARD_PATH: ${OLD_DASHBOARD:-/dashboard/ (дефолт)} -> $NEW_DASHBOARD"
ok "XRAY_SUBSCRIPTION_PATH: ${OLD_SUB:-sub (дефолт)} -> $NEW_SUB"

echo
warn "Рестартую Marzban — активные VPN-подключения (сама нода) не тронет."
warn "/status в боте всегда спрашивает ссылку у панели заново — актуальную покажет верно."
warn "НО: уже разосланные/забукмарканные клиентами старые ссылки (/sub/старый-путь/...)"
warn "перестанут работать. Пока клиентов 0 — не проблема. Ротируя ЕЩЁ РАЗ на боевой"
warn "базе — разошли клиентам новую ссылку заранее (кнопка в боте/рассылка)."
( cd "$MARZBAN_DIR" && docker compose up -d )

echo
echo "Новый дашборд:  <домен панели>${NEW_DASHBOARD}"
echo "Сохрани эти пути (менеджер паролей) — без них в панель зайти не через что."
