#!/usr/bin/env bash
# panel/caddy/setup.sh — авто-разворот Caddy перед Marzban: dashboard-домен
# остаётся за Cloudflare (orange), домен выдачи подписки уходит на свой
# Let's Encrypt серт мимо CF. Идемпотентно — можно гонять повторно.
#
# Запуск на мастер-сервере Marzban, от root:
#   MON_DOMAIN=mon.example.com SUB_DOMAIN=sub.example.com ./setup.sh
#
# Переменные:
#   MON_DOMAIN     — домен дашборда/API, проксируется через Cloudflare (обязателен)
#   SUB_DOMAIN     — домен выдачи подписки, DNS-only в Cloudflare (обязателен)
#   MON_CERT_DIR   — где лежит серт для MON_DOMAIN (default /var/lib/marzban/certs,
#                    ожидает fullchain.pem + key.pem — обычно Cloudflare Origin CA)
#   MARZBAN_ENV    — путь к .env Marzban (default /opt/marzban/.env)

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
info() { echo -e "${CYAN}▸${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "Запускай от root"
: "${MON_DOMAIN:?укажи MON_DOMAIN=mon.example.com}"
: "${SUB_DOMAIN:?укажи SUB_DOMAIN=sub.example.com}"
MON_CERT_DIR="${MON_CERT_DIR:-/var/lib/marzban/certs}"
MARZBAN_ENV="${MARZBAN_ENV:-/opt/marzban/.env}"
ACME_WEBROOT="/var/www/acme-challenge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -f "$MON_CERT_DIR/fullchain.pem" ] && [ -f "$MON_CERT_DIR/key.pem" ] \
    || die "Нет $MON_CERT_DIR/{fullchain,key}.pem — положи серт для $MON_DOMAIN туда сперва"

# ─── 1. Caddy + certbot + acl ────────────────────────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
    info "Ставлю Caddy"
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update >/dev/null && apt-get install -y caddy >/dev/null
else
    ok "Caddy уже стоит ($(caddy version | head -1))"
fi
command -v certbot >/dev/null 2>&1 || apt-get install -y certbot >/dev/null
command -v setfacl >/dev/null 2>&1 || apt-get install -y acl >/dev/null

# ─── 2. Marzban больше не биндит публичный порт напрямую ────────────────────
if [ -f "$MARZBAN_ENV" ]; then
    sed -i \
        -e 's/^UVICORN_HOST\s*=.*/UVICORN_HOST = "127.0.0.1"/' \
        -e 's/^UVICORN_PORT\s*=.*/UVICORN_PORT = 8001/' \
        -e 's/^\(UVICORN_SSL_CERTFILE\)/# \1/' \
        -e 's/^\(UVICORN_SSL_KEYFILE\)/# \1/' \
        -e "s#^XRAY_SUBSCRIPTION_URL_PREFIX\s*=.*#XRAY_SUBSCRIPTION_URL_PREFIX = \"https://${SUB_DOMAIN}\"#" \
        "$MARZBAN_ENV"
    grep -q '^UVICORN_HOST' "$MARZBAN_ENV" || echo 'UVICORN_HOST = "127.0.0.1"' >> "$MARZBAN_ENV"
    grep -q '^UVICORN_PORT' "$MARZBAN_ENV" || echo 'UVICORN_PORT = 8001' >> "$MARZBAN_ENV"
    grep -q '^XRAY_SUBSCRIPTION_URL_PREFIX' "$MARZBAN_ENV" || echo "XRAY_SUBSCRIPTION_URL_PREFIX = \"https://${SUB_DOMAIN}\"" >> "$MARZBAN_ENV"
    ok "Marzban .env поправлен ($MARZBAN_ENV) — не забудь docker compose up -d (не restart)"
else
    warn "Не нашёл $MARZBAN_ENV — поправь UVICORN_HOST/PORT и XRAY_SUBSCRIPTION_URL_PREFIX вручную"
fi

# ─── 3. Освободить 443 (локальный xray мастера может успеть его перехватить) ─
XRAY_PID="$(ss -tlnp 2>/dev/null | grep ':443 ' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)"
if [ -n "${XRAY_PID:-}" ]; then
    warn "Порт 443 занят (pid $XRAY_PID) — убиваю, это локальный xray мастера, не возит трафик"
    kill -9 "$XRAY_PID" 2>/dev/null || true
fi
systemctl stop caddy 2>/dev/null || true

# ─── 4. Серт для SUB_DOMAIN (если ещё нет) ───────────────────────────────────
mkdir -p "$ACME_WEBROOT"
if [ ! -f "/etc/letsencrypt/live/$SUB_DOMAIN/fullchain.pem" ]; then
    info "Получаю Let's Encrypt серт для $SUB_DOMAIN (порт 80 должен быть свободен)"
    certbot certonly --standalone --non-interactive --agree-tos \
        --register-unsafely-without-email -d "$SUB_DOMAIN"
else
    ok "Серт для $SUB_DOMAIN уже есть"
fi

# ─── 5. ACL — caddy читает root-only файлы certbot ───────────────────────────
setfacl -d -m u:caddy:r "/etc/letsencrypt/archive/$SUB_DOMAIN/" 2>/dev/null || true
setfacl -m u:caddy:r "/etc/letsencrypt/archive/$SUB_DOMAIN"/privkey*.pem "/etc/letsencrypt/archive/$SUB_DOMAIN"/fullchain*.pem 2>/dev/null || true
setfacl -m u:caddy:x /etc/letsencrypt/archive /etc/letsencrypt/live 2>/dev/null || true
ok "ACL для caddy выставлены"

# ─── 6. Продление — webroot, не standalone (тот конфликтует с Caddy) ────────
RENEWAL_CONF="/etc/letsencrypt/renewal/$SUB_DOMAIN.conf"
if [ -f "$RENEWAL_CONF" ] && ! grep -q '^\[\[webroot_map\]\]' "$RENEWAL_CONF"; then
    sed -i 's/^authenticator = standalone/authenticator = webroot/' "$RENEWAL_CONF"
    cat >> "$RENEWAL_CONF" <<EOF
[[webroot_map]]
$SUB_DOMAIN = $ACME_WEBROOT
EOF
    ok "Продление переключено на webroot"
fi

mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-caddy.sh <<EOF
#!/bin/sh
setfacl -m u:caddy:r /etc/letsencrypt/archive/$SUB_DOMAIN/privkey*.pem \\
                     /etc/letsencrypt/archive/$SUB_DOMAIN/fullchain*.pem 2>/dev/null
systemctl reload caddy
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-caddy.sh
ok "Deploy-hook на продление настроен"

# ─── 7. Caddyfile из шаблона ──────────────────────────────────────────────────
sed \
    -e "s#__MON_DOMAIN__#${MON_DOMAIN}#g" \
    -e "s#__SUB_DOMAIN__#${SUB_DOMAIN}#g" \
    -e "s#__MON_CERT_DIR__#${MON_CERT_DIR}#g" \
    "$SCRIPT_DIR/Caddyfile.tmpl" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile || die "Caddyfile не прошёл валидацию"
ok "Caddyfile сгенерирован и провалидирован"

# ─── 8. Запуск ────────────────────────────────────────────────────────────────
systemctl enable --now caddy
systemctl reload caddy
ok "Caddy запущен"

echo ""
info "Проверь: curl -sI https://${MON_DOMAIN}/  и  curl -sI https://${SUB_DOMAIN}/"
info "И перекати Marzban: cd \$(dirname $MARZBAN_ENV) && docker compose up -d"
info "В Cloudflare: $SUB_DOMAIN должен быть DNS only (серое облако), $MON_DOMAIN — Proxied (оранжевое)"
