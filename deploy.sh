#!/usr/bin/env bash
# deploy.sh — первый деплой и обновление бота 3xui-client-bot.
#
# Первый раз на сервере:
#   git clone https://github.com/ВАШ_ЛОГИН/3xui-client-bot
#   cd 3xui-client-bot && ./deploy.sh
#
# Обновление:
#   cd 3xui-client-bot && ./deploy.sh

set -euo pipefail

# ─── цвета ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
info() { echo -e "${CYAN}▸${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }
header() { echo -e "\n${BOLD}$*${NC}"; }

echo ""
echo -e "${BOLD}═══════════════════════════════════════${NC}"
echo -e "${BOLD}   3xui-client-bot  —  deploy script   ${NC}"
echo -e "${BOLD}═══════════════════════════════════════${NC}"

# ─── вспомогательные функции ввода ───────────────────────────────────────────

# ask VAR "Описание" "default"  →  читает строку, сохраняет в VAR
ask() {
    local _var="$1" _prompt="$2" _default="${3:-}"
    local _hint=""
    [ -n "$_default" ] && _hint=" [${_default}]"
    local _val=""
    while true; do
        read -rp "  ${_prompt}${_hint}: " _val
        _val="${_val:-$_default}"
        if [ -n "$_val" ]; then
            printf -v "$_var" '%s' "$_val"
            return
        fi
        warn "Значение обязательно"
    done
}

# ask_secret VAR "Описание"  →  ввод без эха (пароль/токен)
ask_secret() {
    local _var="$1" _prompt="$2"
    local _val=""
    while true; do
        read -rsp "  ${_prompt}: " _val; echo ""
        if [ -n "$_val" ]; then
            printf -v "$_var" '%s' "$_val"
            return
        fi
        warn "Значение обязательно"
    done
}

# ask_optional VAR "Описание" "default"  →  пустой ввод допустим
ask_optional() {
    local _var="$1" _prompt="$2" _default="${3:-}"
    local _hint=""
    [ -n "$_default" ] && _hint=" [${_default}]"
    local _val=""
    read -rp "  ${_prompt}${_hint}: " _val
    printf -v "$_var" '%s' "${_val:-$_default}"
}

# ask_secret_optional VAR "Описание"  →  пустой ввод = пропустить
ask_secret_optional() {
    local _var="$1" _prompt="$2"
    read -rsp "  ${_prompt} (Enter — пропустить): " "$_var"; echo ""
}

# ─── зависимости ─────────────────────────────────────────────────────────────
header "1 / 5  Проверка зависимостей"
command -v git    >/dev/null 2>&1 || die "git не найден: apt install git"
command -v docker >/dev/null 2>&1 || die "docker не найден: https://docs.docker.com/engine/install/"

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "docker compose не найден. Установите Docker Compose v2."
fi
ok "git, docker, compose — всё есть"

# ─── обновление кода ─────────────────────────────────────────────────────────
header "2 / 5  Обновление кода"
if [ -d ".git" ]; then
    git pull --ff-only && ok "git pull выполнен"
else
    warn "Папка .git не найдена — пропускаем git pull"
fi

# ─── настройка .env ──────────────────────────────────────────────────────────
header "3 / 5  Конфигурация .env"

if [ -f ".env" ]; then
    echo ""
    read -rp "  .env уже существует. Перезаписать настройки? [y/N] " REWRITE
    if [[ ! "${REWRITE:-N}" =~ ^[Yy]$ ]]; then
        info "Пропускаем настройку, используем существующий .env"
        SKIP_SETUP=1
    else
        SKIP_SETUP=0
    fi
else
    SKIP_SETUP=0
fi

if [ "${SKIP_SETUP:-0}" -eq 0 ]; then
    echo ""
    echo -e "  Введите параметры бота. ${YELLOW}Токены вводятся без эха (не отображаются).${NC}"
    echo ""

    # ── Telegram ──────────────────────────────────────────────────────────────
    echo -e "  ${BOLD}── Telegram ──────────────────────────────────────────${NC}"
    echo "  Токен от @BotFather (вид: 123456789:AAH...)"
    ask_secret  BOT_TOKEN    "BOT_TOKEN"
    echo ""
    echo "  Ваш Telegram ID — узнайте у @userinfobot. Несколько — через запятую."
    ask         ADMIN_IDS    "ADMIN_IDS"
    echo ""
    echo "  ID чата/пользователя для сообщений «Связаться» (обычно = ваш ADMIN_IDS)."
    ask_optional SUPPORT_CHAT_ID "SUPPORT_CHAT_ID" "$ADMIN_IDS"

    echo ""
    # ── Бэкенд панели ─────────────────────────────────────────────────────────
    echo -e "  ${BOLD}── Какая панель? ──────────────────────────────────────${NC}"
    echo "  1) 3x-ui"
    echo "  2) Marzban"
    ask PANEL_BACKEND_CHOICE "Номер" "1"
    case "$PANEL_BACKEND_CHOICE" in
        2) PANEL_BACKEND="marzban" ;;
        *) PANEL_BACKEND="xui" ;;
    esac
    ok "Бэкенд: $PANEL_BACKEND"

    # значения по умолчанию — чтобы .env-heredoc ниже не падал на неопределённых
    # переменных, если ветка для другого бэкенда не выполнялась
    XUI_BASE_URL=""; XUI_AUTH="token"; XUI_API_TOKEN=""; XUI_USERNAME=""; XUI_PASSWORD=""; XUI_2FA_SECRET=""
    SUB_URL_TEMPLATE=""
    MARZBAN_URL=""; MARZBAN_USERNAME=""; MARZBAN_PASSWORD=""; MARZBAN_RESET_STRATEGY="month"
    MARZBAN_PROXIES=""; MARZBAN_INBOUNDS=""
    NODE_PROVISION_ENABLED="false"; NODE_PROVISION_PORT="8443"
    NODE_TOKEN_TTL_SECONDS="1800"; MARZBAN_CERT_DIR="/var/lib/marzban/certs"

    echo ""
    if [ "$PANEL_BACKEND" = "marzban" ]; then
        # ── Marzban ───────────────────────────────────────────────────────────
        echo -e "  ${BOLD}── Marzban панель ──────────────────────────────────${NC}"
        echo "  URL главной панели: https://mon.your-domain.tld (без слэша на конце)"
        ask         MARZBAN_URL      "MARZBAN_URL"
        echo ""
        echo "  Логин админа панели."
        ask         MARZBAN_USERNAME "MARZBAN_USERNAME"
        echo ""
        echo "  Пароль админа панели."
        ask_secret  MARZBAN_PASSWORD "MARZBAN_PASSWORD"
        echo ""
        echo "  Стратегия сброса трафика клиента: month | no_reset."
        ask_optional MARZBAN_RESET_STRATEGY "MARZBAN_RESET_STRATEGY" "month"

        echo ""
        echo -e "  ${BOLD}── Какие инбаунды получают НОВЫХ клиентов ────────────${NC}"
        echo "  Спрашиваю у панели список инбаундов, чтобы не гадать вслепую —"
        echo "  оставить пустым (Enter вместо номеров) означает ВСЕ инбаунды панели,"
        echo "  включая шаблонный мусор вроде неиспользуемого Shadowsocks."
        # Живой запрос к панели: список инбаундов -> интерактивный выбор номеров ->
        # готовые MARZBAN_PROXIES/MARZBAN_INBOUNDS. Меню и вопрос — в stderr, чтобы
        # $(...) поймал только два финальных JSON на stdout.
        INBOUND_PICK_OUT="$(python3 - "$MARZBAN_URL" "$MARZBAN_USERNAME" "$MARZBAN_PASSWORD" <<'PY'
import json, sys, urllib.error, urllib.parse, urllib.request


def eprint(*a):
    print(*a, file=sys.stderr)


base, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def op(req):
    req.add_header("User-Agent", ua)
    return urllib.request.urlopen(req, timeout=15)


try:
    d = urllib.parse.urlencode({"username": user, "password": pw}).encode()
    tok = json.load(op(urllib.request.Request(base + "/api/admin/token", data=d, method="POST"))).get("access_token")
    if not tok:
        eprint("AUTH_FAILED")
        sys.exit(2)
    rq = urllib.request.Request(base + "/api/inbounds")
    rq.add_header("Authorization", "Bearer " + tok)
    data = json.load(op(rq))
except Exception as e:  # noqa: BLE001
    eprint(f"FETCH_FAILED: {e}")
    sys.exit(2)

pairs = []
eprint("  Найденные инбаунды:")
for proto, items in data.items():
    for it in items:
        pairs.append((proto, it["tag"]))
        eprint(f"    {len(pairs)}) {proto} / {it['tag']}")

if not pairs:
    eprint("  Панель не вернула ни одного инбаунда — сначала опиши REALITY-инбаунд")
    eprint("  в самой панели (Core Config), потом перезапусти deploy.sh.")
    sys.exit(3)

eprint("")
sel = input("  Номера через запятую (обычно только REALITY), Enter — взять все: ").strip()

if sel:
    try:
        idxs = [int(x.strip()) for x in sel.split(",") if x.strip()]
        chosen = [pairs[i - 1] for i in idxs if 1 <= i <= len(pairs)]
        if not chosen:
            raise ValueError
    except (ValueError, IndexError):
        eprint("  Не разобрал номера — беру все инбаунды.")
        chosen = pairs
else:
    chosen = pairs

proxies, inbounds = {}, {}
for proto, tag in chosen:
    inbounds.setdefault(proto, []).append(tag)
    if proto not in proxies:
        proxies[proto] = {"flow": "xtls-rprx-vision"} if proto == "vless" else {}

print(json.dumps(proxies))
print(json.dumps(inbounds))
PY
)"
        INBOUND_PICK_STATUS=$?
        if [ "$INBOUND_PICK_STATUS" -eq 0 ]; then
            MARZBAN_PROXIES="$(echo "$INBOUND_PICK_OUT" | sed -n 1p)"
            MARZBAN_INBOUNDS="$(echo "$INBOUND_PICK_OUT" | sed -n 2p)"
            ok "Настроено: proxies=$MARZBAN_PROXIES"
            ok "           inbounds=$MARZBAN_INBOUNDS"
        else
            warn "Не смог достучаться до панели за списком инбаундов — впиши JSON руками."
            echo "  JSON proxies, например {\"vless\":{\"flow\":\"xtls-rprx-vision\"}}"
            ask_optional MARZBAN_PROXIES "MARZBAN_PROXIES" "{\"vless\":{\"flow\":\"xtls-rprx-vision\"}}"
            echo "  JSON inbounds, например {\"vless\":[\"VLESS REALITY\"]}"
            ask_optional MARZBAN_INBOUNDS "MARZBAN_INBOUNDS" "{\"vless\":[\"VLESS REALITY\"]}"
        fi

        echo ""
        echo -e "  ${BOLD}── Авторазвёртывание нод («🖥 Добавить сервер») ───────${NC}"
        echo "  Открывает у бота ОДИН входящий HTTP-порт (обычно он только long-polling,"
        echo "  без входящих портов вообще) — осознанное исключение, см. node/README.md."
        read -rp "  Включить кнопку «Добавить сервер» в админке? [y/N] " NODE_PROVISION_YN
        if [[ "${NODE_PROVISION_YN:-N}" =~ ^[Yy]$ ]]; then
            NODE_PROVISION_ENABLED="true"
            echo "  Порт (Cloudflare проксирует без доп. настройки: 443/2053/2083/2087/2096/8443):"
            ask_optional NODE_PROVISION_PORT "NODE_PROVISION_PORT" "8443"
            ask_optional NODE_TOKEN_TTL_SECONDS "Время жизни токена, сек" "1800"
            echo "  Каталог с cert панели (fullchain.pem/key.pem) НА ХОСТЕ — нужен TLS на"
            echo "  порту провижининга за Cloudflare, переиспользуем cert панели:"
            ask_optional MARZBAN_CERT_DIR "MARZBAN_CERT_DIR" "/var/lib/marzban/certs"
        fi
    else
        # ── 3X-UI ─────────────────────────────────────────────────────────────────
        echo -e "  ${BOLD}── 3X-UI панель ──────────────────────────────────────${NC}"
        echo "  Базовый URL панели: https://домен:порт/секретный-путь"
        echo "  (без /panel на конце; секретный путь — из настроек панели)"
        ask         XUI_BASE_URL  "XUI_BASE_URL"
        echo ""
        echo "  API-токен панели (Настройки → API Token в 3X-UI)."
        ask_secret  XUI_API_TOKEN "XUI_API_TOKEN"
        echo ""
        echo "  Если используете логин/пароль вместо токена — введите логин (иначе Enter)."
        ask_optional XUI_USERNAME "XUI_USERNAME (Enter — пропустить)"
        if [ -n "$XUI_USERNAME" ]; then
            ask_secret_optional XUI_PASSWORD "XUI_PASSWORD"
        else
            XUI_PASSWORD=""
        fi
        echo ""
        echo "  2FA-секрет панели, если включена двухфакторка (иначе Enter)."
        ask_secret_optional XUI_2FA_SECRET "XUI_2FA_SECRET"

        echo ""
        # ── Подписка ───────────────────────────────────────────────────────────────
        echo -e "  ${BOLD}── Ссылка-подписка ───────────────────────────────────${NC}"
        echo "  Шаблон sub-ссылки. Возьмите рабочую ссылку из панели"
        echo "  (Clients → скопировать ссылку подписки) и замените subId на {subId}."
        echo "  Пример: https://your-domain.tld:ПОРТ/путь/{subId}"
        ask         SUB_URL_TEMPLATE "SUB_URL_TEMPLATE"
    fi

    echo ""
    # ── Тариф ──────────────────────────────────────────────────────────────────
    echo -e "  ${BOLD}── Тариф и оплата ────────────────────────────────────${NC}"
    ask_optional PLAN_DAYS    "Срок подписки, дней"         "30"
    ask_optional DEFAULT_PRICE "Цена тарифа (₽)"            "199"
    echo "  Реквизиты для перевода (банк + номер карты + имя):"
    ask_optional DEFAULT_REQUISITES "DEFAULT_REQUISITES"    "Сбербанк 0000 0000 0000 0000 (Имя Ф.)"

    echo ""
    # ── Напоминания ────────────────────────────────────────────────────────────
    echo -e "  ${BOLD}── Напоминания ───────────────────────────────────────${NC}"
    ask_optional REMIND_DAYS_BEFORE "За сколько дней напоминать (через запятую)" "3,1,0"
    ask_optional REMIND_HOUR        "Час отправки напоминаний (0–23, UTC+3 МСК)" "11"

    echo ""
    # ── Бэкап в R2 (опционально) ─────────────────────────────────────────────────
    echo -e "  ${BOLD}── Бэкап в Cloudflare R2 (опционально) ───────────────${NC}"
    BACKUP_ENABLED=false
    R2_ENDPOINT=""; R2_BUCKET=""; R2_ACCESS_KEY_ID=""; R2_SECRET_ACCESS_KEY=""; BACKUP_AGE_PUBKEY=""
    XUI_DB_HOST_PATH=""; MARZBAN_DB_HOST_PATH=""
    read -rp "  Включить ежедневный бэкап БД в R2? [y/N] " BK
    if [[ "${BK:-N}" =~ ^[Yy]$ ]]; then
        BACKUP_ENABLED=true
        echo "  Endpoint: https://<account_id>.r2.cloudflarestorage.com"
        ask         R2_ENDPOINT  "R2_ENDPOINT"
        ask_optional R2_BUCKET   "Имя бакета"  "vpn-backups"
        ask         R2_ACCESS_KEY_ID "R2 Access Key ID"
        ask_secret  R2_SECRET_ACCESS_KEY "R2 Secret Access Key"
        echo "  age-публичный ключ для шифрования (age1...), Enter — без шифрования:"
        ask_optional BACKUP_AGE_PUBKEY "BACKUP_AGE_PUBKEY"
        if [ "$PANEL_BACKEND" = "xui" ]; then
            echo "  Путь к x-ui.db на хосте (Enter — без бэкапа x-ui, только bot.db):"
            ask_optional XUI_DB_HOST_PATH "XUI_DB_HOST_PATH" "/etc/x-ui/x-ui.db"
        else
            echo "  Путь к db.sqlite3 Marzban на хосте (Enter — без бэкапа Marzban, только bot.db):"
            ask_optional MARZBAN_DB_HOST_PATH "MARZBAN_DB_HOST_PATH" "/var/lib/marzban/db.sqlite3"
        fi
    fi

    # ── Запись .env ────────────────────────────────────────────────────────────
    echo ""
    info "Записываю .env…"

    # Определяем XUI_AUTH
    XUI_AUTH="token"
    [ -n "${XUI_USERNAME:-}" ] && XUI_AUTH="login"

    cat > .env <<EOF
# ===== Telegram =====
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
SUPPORT_CHAT_ID=${SUPPORT_CHAT_ID}

# ===== Бэкенд панели: xui | marzban =====
PANEL_BACKEND=${PANEL_BACKEND}

# ===== 3X-UI (используется, если PANEL_BACKEND=xui) =====
XUI_BASE_URL=${XUI_BASE_URL}
XUI_AUTH=${XUI_AUTH}
XUI_API_TOKEN=${XUI_API_TOKEN}
XUI_USERNAME=${XUI_USERNAME:-}
XUI_PASSWORD=${XUI_PASSWORD:-}
XUI_2FA_SECRET=${XUI_2FA_SECRET:-}

# ===== Marzban (используется, если PANEL_BACKEND=marzban) =====
MARZBAN_URL=${MARZBAN_URL:-}
MARZBAN_USERNAME=${MARZBAN_USERNAME:-}
MARZBAN_PASSWORD=${MARZBAN_PASSWORD:-}
MARZBAN_RESET_STRATEGY=${MARZBAN_RESET_STRATEGY:-month}
MARZBAN_PROXIES=${MARZBAN_PROXIES:-}
MARZBAN_INBOUNDS=${MARZBAN_INBOUNDS:-}

# ===== Авторазвёртывание нод («🖥 Добавить сервер» в админке) =====
NODE_PROVISION_ENABLED=${NODE_PROVISION_ENABLED:-false}
NODE_PROVISION_PORT=${NODE_PROVISION_PORT:-8443}
NODE_TOKEN_TTL_SECONDS=${NODE_TOKEN_TTL_SECONDS:-1800}
MARZBAN_CERT_DIR=${MARZBAN_CERT_DIR:-/var/lib/marzban/certs}

# ===== Подписка (используется, если PANEL_BACKEND=xui — Marzban отдаёт
# subscription_url сам, шаблон ему не нужен) =====
PLAN_DAYS=${PLAN_DAYS}
PLAN_TRAFFIC_GB=150
DEFAULT_INBOUND_IDS=1
CLIENT_FLOW=xtls-rprx-vision
SUB_URL_TEMPLATE=${SUB_URL_TEMPLATE}

# ===== Оплата =====
DEFAULT_PRICE=${DEFAULT_PRICE}
DEFAULT_REQUISITES=${DEFAULT_REQUISITES}

# ===== Система =====
DB_PATH=data/bot.db
REMIND_DAYS_BEFORE=${REMIND_DAYS_BEFORE}
REMIND_HOUR=${REMIND_HOUR}

# ===== Бэкап в R2 =====
BACKUP_ENABLED=${BACKUP_ENABLED}
R2_ENDPOINT=${R2_ENDPOINT}
R2_BUCKET=${R2_BUCKET}
R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
BACKUP_AGE_PUBKEY=${BACKUP_AGE_PUBKEY}
XUI_DB_HOST_PATH=${XUI_DB_HOST_PATH}
MARZBAN_DB_HOST_PATH=${MARZBAN_DB_HOST_PATH}
EOF

    ok ".env создан"
fi

# ─── права на файлы ───────────────────────────────────────────────────────────
chmod 600 .env
mkdir -p data && chmod 700 data
# Контейнер работает от uid 10001 (non-root) — отдаём ему владение data/,
# иначе он не сможет писать SQLite в смонтированный том.
chown -R 10001:10001 data 2>/dev/null \
    && ok "chmod 600 .env, chmod 700 data/, chown 10001 data/" \
    || warn "chmod 600 .env, chmod 700 data/ (chown пропущен — нужен root)"

# x-ui.db для бэкапа: путь берём из .env (XUI_DB_HOST_PATH). Если задан —
# обязан существовать как ФАЙЛ, иначе Docker создаст на его месте битую директорию
# и может сломать саму панель. Поэтому при отсутствии — аварийно останавливаемся.
XUI_DB=$(grep -E '^XUI_DB_HOST_PATH=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | xargs 2>/dev/null || true)
if [ -n "$XUI_DB" ]; then
    if [ ! -f "$XUI_DB" ]; then
        die "XUI_DB_HOST_PATH=$XUI_DB не найден (или не файл). Укажи верный путь в .env
     или очисти XUI_DB_HOST_PATH (тогда x-ui не бэкапится). Иначе Docker создаст
     битую директорию на месте файла."
    fi
    # Дать non-root контейнеру (uid 10001) право читать x-ui.db.
    command -v setfacl >/dev/null 2>&1 || apt-get install -y acl >/dev/null 2>&1 || true
    XUI_DIR=$(dirname "$XUI_DB")
    if command -v setfacl >/dev/null 2>&1 \
       && setfacl -m u:10001:r "$XUI_DB" 2>/dev/null \
       && setfacl -m u:10001:x "$XUI_DIR" 2>/dev/null; then
        ok "ACL: uid 10001 может читать x-ui.db (для бэкапа)"
    elif chmod o+r "$XUI_DB" 2>/dev/null && chmod o+x "$XUI_DIR" 2>/dev/null; then
        warn "ACL недоступен — выставил o+r на x-ui.db (читаемо для всех на хосте)"
    else
        warn "Не смог дать доступ к $XUI_DB — бэкап x-ui может не сработать (нужен root)"
    fi
fi

# ─── итоговая проверка .env ───────────────────────────────────────────────────
MISSING=()
check_var() {
    local val
    val=$(grep -E "^${1}=" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs 2>/dev/null || true)
    if [[ -z "$val" || "$val" == "CHANGE_ME" || "$val" =~ ^123456: ]]; then
        MISSING+=("$1")
    fi
}
check_var BOT_TOKEN
check_var ADMIN_IDS

# читаем PANEL_BACKEND из .env напрямую — при повторном запуске (SKIP_SETUP=1,
# .env уже существует) блок настройки выше не выполнялся, шелл-переменная
# $PANEL_BACKEND могла остаться пустой.
ENV_PANEL_BACKEND=$(grep -E '^PANEL_BACKEND=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs 2>/dev/null || true)
if [ "${ENV_PANEL_BACKEND:-xui}" = "marzban" ]; then
    check_var MARZBAN_URL
    check_var MARZBAN_USERNAME
    check_var MARZBAN_PASSWORD
else
    check_var XUI_BASE_URL
    check_var XUI_API_TOKEN
    check_var SUB_URL_TEMPLATE
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    warn "Не заполнены обязательные переменные:"
    for v in "${MISSING[@]}"; do echo "    • $v"; done
    echo ""
    read -rp "  Продолжить всё равно? [y/N] " CONT
    [[ "${CONT:-N}" =~ ^[Yy]$ ]] || die "Заполните .env и запустите ./deploy.sh снова"
fi

# ─── сборка и запуск ─────────────────────────────────────────────────────────
header "4 / 5  Сборка и запуск контейнера"
$COMPOSE up -d --build
ok "Контейнер запущен"

# ─── статус и логи ───────────────────────────────────────────────────────────
header "5 / 5  Статус"
echo ""
$COMPOSE ps
echo ""
info "Последние логи:"
$COMPOSE logs --tail=25 bot

echo ""
echo -e "${GREEN}${BOLD}═══════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}   Деплой завершён!                    ${NC}"
echo -e "${GREEN}${BOLD}═══════════════════════════════════════${NC}"
echo ""
echo "Команды:"
echo "  $COMPOSE logs -f bot    — следить за логами"
echo "  $COMPOSE restart bot    — перезапустить"
echo "  $COMPOSE down           — остановить"
echo "  ./deploy.sh             — обновить до последней версии"
echo ""
