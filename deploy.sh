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

# ===== 3X-UI =====
XUI_BASE_URL=${XUI_BASE_URL}
XUI_AUTH=${XUI_AUTH}
XUI_API_TOKEN=${XUI_API_TOKEN}
XUI_USERNAME=${XUI_USERNAME:-}
XUI_PASSWORD=${XUI_PASSWORD:-}
XUI_2FA_SECRET=${XUI_2FA_SECRET:-}

# ===== Подписка =====
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
EOF

    ok ".env создан"
fi

# ─── права на файлы ───────────────────────────────────────────────────────────
chmod 600 .env
mkdir -p data && chmod 700 data
ok "chmod 600 .env, chmod 700 data/"

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
check_var XUI_BASE_URL
check_var XUI_API_TOKEN
check_var SUB_URL_TEMPLATE

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
