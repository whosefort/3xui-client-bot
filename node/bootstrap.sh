#!/usr/bin/env bash
#
# bootstrap.sh — автонастройка НОВОГО VPS как ноды Marzban.
#
# Что делает (запуск под root на чистом Ubuntu/Debian):
#   1. Ставит Docker.
#   2. Тянет client-cert главной панели (GET /api/node/settings).
#   3. Поднимает marzban-node (docker, network_mode host, protocol rest).
#   4. UFW: 443 всем (REALITY), node-порты 62050/62051 — ТОЛЬКО с IP панели, SSH.
#   5. Регистрирует ноду в панели (POST /api/node, add_as_new_host=true) —
#      её адрес сам попадёт в Hosts инбаундов, ссылки клиентов поедут на ноду.
#
# Запуск:
#   PANEL_URL=https://mon.your-domain.tld PANEL_USER=admin PANEL_PASS=... \
#     bash bootstrap.sh
#   (креды можно не задавать в env — скрипт спросит; пароль без эха)
#
# ⚠️ REALITY-инбаунд описывается в САМОЙ панели (Core/Hosts) — отдельно, разово.
#    Нода служит то, что панель ей раскатает.

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; NC='\033[0m'
ok(){ echo -e "${G}✓${NC} $*"; }; warn(){ echo -e "${Y}⚠${NC}  $*"; }
die(){ echo -e "${R}✗${NC} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запускай под root."
command -v apt-get >/dev/null 2>&1 || die "Только Debian/Ubuntu (apt)."

# ---------- параметры ----------
PANEL_URL="${PANEL_URL:-}"
PANEL_USER="${PANEL_USER:-}"
PANEL_PASS="${PANEL_PASS:-}"
NODE_NAME="${NODE_NAME:-$(hostname)}"
SERVICE_PORT="${SERVICE_PORT:-62050}"
XRAY_API_PORT="${XRAY_API_PORT:-62051}"
# Порты инбаундов, открыть всем (REALITY обычно 443). Через пробел.
INBOUND_PORTS="${INBOUND_PORTS:-443}"

[ -n "$PANEL_URL" ]  || read -rp "URL главной панели (https://mon.домен): " PANEL_URL
[ -n "$PANEL_USER" ] || read -rp "Логин админа панели: " PANEL_USER
[ -n "$PANEL_PASS" ] || { read -rsp "Пароль админа панели: " PANEL_PASS; echo; }
PANEL_URL="${PANEL_URL%/}"

# ---------- публичный IP ----------
PUBIP="$(curl -fsSL https://api.ipify.org 2>/dev/null || true)"
[ -n "$PUBIP" ] || PUBIP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
[ -n "$PUBIP" ] || die "Не смог определить публичный IP — задай вручную (переменная PUBIP)."
ok "Публичный IP ноды: $PUBIP"

# IP панели (для firewall) — резолвим хост панели
PANEL_HOST="$(echo "$PANEL_URL" | sed -E 's~https?://~~; s~[:/].*$~~')"
PANEL_IP="$(getent hosts "$PANEL_HOST" | awk '{print $1; exit}' || true)"

# ---------- вспомогалка: JSON-поле через python3 ----------
jget(){ python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

# ---------- 1. Docker ----------
echo; ok "Ставлю Docker…"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || die "docker compose v2 не найден."
ok "Docker готов."

# ---------- 2. токен + cert панели ----------
echo; ok "Логинюсь в панель, тяну client-cert…"
TOKEN="$(curl -fsSL -X POST "$PANEL_URL/api/admin/token" \
          -d "username=$PANEL_USER" -d "password=$PANEL_PASS" | jget access_token)"
[ -n "$TOKEN" ] || die "Логин в панель не удался (проверь URL/логин/пароль)."

mkdir -p /var/lib/marzban-node
curl -fsSL "$PANEL_URL/api/node/settings" -H "Authorization: Bearer $TOKEN" \
  | jget certificate > /var/lib/marzban-node/ssl_client_cert.pem
[ -s /var/lib/marzban-node/ssl_client_cert.pem ] || die "Пустой cert от панели."
head -1 /var/lib/marzban-node/ssl_client_cert.pem | grep -q "BEGIN CERT" \
  || die "cert панели не похож на PEM."
ok "Cert панели сохранён."

# ---------- 3. marzban-node (docker) ----------
echo; ok "Поднимаю marzban-node…"
mkdir -p /opt/marzban-node
cat > /opt/marzban-node/docker-compose.yml <<EOF
services:
  marzban-node:
    image: gozargah/marzban-node:latest
    restart: always
    network_mode: host
    environment:
      SERVICE_PORT: "${SERVICE_PORT}"
      XRAY_API_PORT: "${XRAY_API_PORT}"
      SERVICE_PROTOCOL: "rest"
      SSL_CLIENT_CERT_FILE: "/var/lib/marzban-node/ssl_client_cert.pem"
    volumes:
      - /var/lib/marzban-node:/var/lib/marzban-node
EOF
( cd /opt/marzban-node && docker compose up -d )
ok "marzban-node запущен."

# ---------- 4. firewall ----------
echo; ok "Настраиваю UFW…"
apt-get install -y ufw >/dev/null 2>&1 || true
ufw allow 22/tcp comment 'SSH' >/dev/null
for p in $INBOUND_PORTS; do ufw allow "${p}/tcp" comment 'inbound' >/dev/null; done
if [ -n "$PANEL_IP" ]; then
  ufw allow proto tcp from "$PANEL_IP" to any port "$SERVICE_PORT" comment 'panel-node' >/dev/null
  ufw allow proto tcp from "$PANEL_IP" to any port "$XRAY_API_PORT" comment 'panel-xray' >/dev/null
  ok "node-порты $SERVICE_PORT/$XRAY_API_PORT открыты только для панели ($PANEL_IP)"
else
  warn "Не срезолвил IP панели ($PANEL_HOST) — открываю node-порты ВСЕМ (сузь потом вручную)."
  ufw allow "${SERVICE_PORT}/tcp" >/dev/null; ufw allow "${XRAY_API_PORT}/tcp" >/dev/null
fi
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
ok "UFW включён."

# ---------- 5. регистрация в панели ----------
echo; ok "Регистрирую ноду в панели…"
REG="$(curl -fsSL -X POST "$PANEL_URL/api/node" \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d "{\"name\":\"${NODE_NAME}\",\"address\":\"${PUBIP}\",\"port\":${SERVICE_PORT},\"api_port\":${XRAY_API_PORT},\"add_as_new_host\":true}" || true)"
NODE_ID="$(echo "$REG" | jget id)"
if [ -n "$NODE_ID" ]; then
  ok "Нода зарегистрирована: id=$NODE_ID name=$NODE_NAME address=$PUBIP"
else
  warn "Авто-регистрация не удалась (возможно, нода с таким адресом уже есть). Ответ: ${REG:0:200}"
  warn "Зарегистрируй руками в панели: Node Settings → Add Node → address=$PUBIP, port=$SERVICE_PORT, api_port=$XRAY_API_PORT."
fi

echo
echo -e "${G}ГОТОВО.${NC}"
echo "Проверь в панели: нода должна стать 'connected' за ~10-30 сек."
echo "Логи ноды:  cd /opt/marzban-node && docker compose logs -f"
echo "Далее: опиши REALITY-инбаунд в САМОЙ панели (Core Config / Hosts) — нода его подхватит,"
echo "а add_as_new_host уже добавил адрес $PUBIP в Hosts, так что ссылки поедут на эту ноду."
