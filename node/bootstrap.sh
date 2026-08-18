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
#   PANEL_URL=https://mon.your-domain.tld bash bootstrap.sh
#   (логин и пароль скрипт спросит; пароль без эха)
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
# Реальный IP панели. Обычно определяется автоматически, но если панель за CDN
# (Cloudflare и т.п.) — DNS вернёт IP CDN, а входящие соединения панель→нода
# придут с реального origin-IP. Тогда его обязательно надо задать вручную.
PANEL_IP="${PANEL_IP:-}"
# Порты инбаундов, открыть всем. 443 (обычный HTTPS) + 8443 (наш REALITY).
INBOUND_PORTS="${INBOUND_PORTS:-443 8443}"

[ -n "$PANEL_URL" ]  || read -rp "URL главной панели (https://mon.домен): " PANEL_URL
[ -n "$PANEL_USER" ] || read -rp "Логин админа панели: " PANEL_USER
[ -n "$PANEL_PASS" ] || { read -rsp "Пароль админа панели: " PANEL_PASS; echo; }
PANEL_URL="${PANEL_URL%/}"

# ---------- публичный IP ----------
PUBIP="$(curl -fsSL https://api.ipify.org 2>/dev/null || true)"
[ -n "$PUBIP" ] || PUBIP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
[ -n "$PUBIP" ] || die "Не смог определить публичный IP — задай вручную (переменная PUBIP)."
ok "Публичный IP ноды: $PUBIP"

# ВАЖНО: если панель спрятана за CDN (Cloudflare/…), DNS вернёт anycast-IP CDN,
# а входящие соединения панель→нода придут с реального origin-IP. В этом случае
# PANEL_IP передаётся явно: доверять первому входящему TCP-соединению небезопасно.
PANEL_HOST="$(echo "$PANEL_URL" | sed -E 's~https?://~~; s~[:/].*$~~')"
PANEL_IP_EXPLICIT=""
if [ -n "$PANEL_IP" ]; then
  PANEL_IP_EXPLICIT=1
else
  # резолвим через python — надёжнее getent (тот может вернуть IPv6 первым)
  PANEL_IP="$(python3 - "$PANEL_HOST" <<'PY' 2>/dev/null || true
import socket, sys
print(socket.gethostbyname(sys.argv[1]))
PY
)"
fi

# Известные диапазоны Cloudflare: их нельзя использовать как source-IP панели.
_is_cloudflare_ip() {
  python3 - "$1" <<'PY'
import ipaddress, sys
try:
    ip = ipaddress.ip_address(sys.argv[1].strip())
except ValueError:
    raise SystemExit(0)
nets = ("173.245.48.0/20","103.21.244.0/22","103.22.200.0/22","103.31.4.0/22",
        "141.101.64.0/18","108.162.192.0/18","190.93.240.0/20","188.114.96.0/20",
        "197.234.240.0/22","198.41.128.0/17","162.158.0.0/15","104.16.0.0/13",
        "104.24.0.0/14","172.64.0.0/13","131.0.72.0/22","2606:4700::/32")
for n in nets:
    if ip in ipaddress.ip_network(n):
        print("yes")
        break
PY
}

[ -n "$PANEL_IP" ] || die "Не смог определить IP панели. Задай PANEL_IP=реальный_origin_IP."
python3 - "$PANEL_IP" <<'PY' || die "PANEL_IP должен быть корректным IPv4/IPv6-адресом."
import ipaddress, sys
ipaddress.ip_address(sys.argv[1])
PY
if [ "$(_is_cloudflare_ip "$PANEL_IP")" = "yes" ]; then
  if [ -n "$PANEL_IP_EXPLICIT" ]; then
    die "PANEL_IP указывает на Cloudflare. Задай реальный origin-IP панели, не CDN-IP."
  fi
  die "DNS панели ($PANEL_HOST) указывает на Cloudflare. Повтори с PANEL_IP=реальный_origin_IP."
fi

# curl-конфиг хранит секреты вне argv; файл доступен только root и удаляется при выходе.
CURL_AUTH_CONFIG="$(mktemp /tmp/marzban-bootstrap-curl.XXXXXX)"
chmod 600 "$CURL_AUTH_CONFIG"
trap 'rm -f -- "$CURL_AUTH_CONFIG"' EXIT
_curl_config_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
_write_curl_auth() {
  : > "$CURL_AUTH_CONFIG"
  printf 'header = "%s"\n' "$(_curl_config_escape "Authorization: Bearer $TOKEN")" >> "$CURL_AUTH_CONFIG"
}

# ---------- вспомогалка: JSON-поле через python3 ----------
jget(){ python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

# ---------- 0. память: swap + отключение мусорных демонов ----------
# На мелких VPS Xray+Docker вместе с fwupd/packagekit легко ловят OOM (нода
# зависает, SSH мрёт). Отключаем ненужные жоры памяти и ставим swap-страховку.
echo; ok "Готовлю память (чистка демонов + swap)…"
systemctl disable --now fwupd fwupd-refresh.timer packagekit >/dev/null 2>&1 || true
if swapon --show 2>/dev/null | grep -q .; then
  ok "swap уже есть."
else
  MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
  if [ "${MEM_MB:-9999}" -lt 2048 ]; then
    if [ ! -e /swapfile ]; then
      fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
      chmod 600 /swapfile
      mkswap /swapfile >/dev/null
      swapon /swapfile
      grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
      ok "swap 2G включён (RAM ${MEM_MB}MB)."
    fi
  else
    ok "RAM ${MEM_MB}MB — swap не нужен."
  fi
fi

# ---------- 1. Docker ----------
echo; ok "Ставлю Docker…"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || die "docker compose v2 не найден."
ok "Docker готов."

# ---------- 2. токен + cert панели ----------
echo; ok "Логинюсь в панель, тяну client-cert…"
{
  printf 'data-urlencode = "%s"\n' "$(_curl_config_escape "username=$PANEL_USER")"
  printf 'data-urlencode = "%s"\n' "$(_curl_config_escape "password=$PANEL_PASS")"
} > "$CURL_AUTH_CONFIG"
TOKEN="$(curl -fsSL -X POST "$PANEL_URL/api/admin/token" --config "$CURL_AUTH_CONFIG" | jget access_token)"
[ -n "$TOKEN" ] || die "Логин в панель не удался (проверь URL/логин/пароль)."

mkdir -p /var/lib/marzban-node
_write_curl_auth
curl -fsSL "$PANEL_URL/api/node/settings" --config "$CURL_AUTH_CONFIG" \
  | jget certificate > /var/lib/marzban-node/ssl_client_cert.pem
chmod 600 /var/lib/marzban-node/ssl_client_cert.pem
[ -s /var/lib/marzban-node/ssl_client_cert.pem ] || die "Пустой cert от панели."
head -1 /var/lib/marzban-node/ssl_client_cert.pem | grep -q "BEGIN CERT" \
  || die "cert панели не похож на PEM."
ok "Cert панели сохранён."

# ---------- 3. marzban-node (docker) ----------
# Образ marzban-node (не путать с xray-core внутри него, см. ниже): по
# умолчанию берём зафиксированную версию из node/MARZBAN_NODE_IMAGE — так
# новые ноды не ловят внезапный апгрейд самого marzban-node между бутстрапами.
# NODE_IMAGE_CHANNEL=latest — осознанный опт-аут на :latest.
NODE_IMAGE_CHANNEL="${NODE_IMAGE_CHANNEL:-}"
if [ -t 0 ] && [ -z "$NODE_IMAGE_CHANNEL" ]; then
  echo ""
  echo "  Образ marzban-node:"
  echo "    1) Стабильная, зафиксированная версия (рекомендуется)"
  echo "    2) Последняя (:latest) — может принести неожиданный апгрейд"
  read -rp "  Выбор [1]: " _img_choice
  [ "${_img_choice:-1}" = "2" ] && NODE_IMAGE_CHANNEL="latest" || NODE_IMAGE_CHANNEL="stable"
fi
NODE_IMAGE_CHANNEL="${NODE_IMAGE_CHANNEL:-stable}"

NODE_IMAGE="${NODE_IMAGE:-}"
NODE_IMAGE_PIN_FILE="$(dirname "${BASH_SOURCE[0]:-$0}")/MARZBAN_NODE_IMAGE"
if [ -z "$NODE_IMAGE" ] && [ "$NODE_IMAGE_CHANNEL" = "stable" ] && [ -f "$NODE_IMAGE_PIN_FILE" ]; then
  NODE_IMAGE="$(tr -d '[:space:]' < "$NODE_IMAGE_PIN_FILE")"
  ok "Образ marzban-node: зафиксированная версия ($NODE_IMAGE)"
fi
if [ -z "$NODE_IMAGE" ]; then
  [ "$NODE_IMAGE_CHANNEL" = "stable" ] && warn "node/MARZBAN_NODE_IMAGE не найден рядом со скриптом — беру :latest."
  NODE_IMAGE="gozargah/marzban-node:latest"
  warn "Образ marzban-node: :latest — версия не зафиксирована, может измениться при следующем бутстрапе."
fi

echo; ok "Поднимаю marzban-node…"
mkdir -p /opt/marzban-node
cat > /opt/marzban-node/docker-compose.yml <<EOF
services:
  marzban-node:
    image: ${NODE_IMAGE}
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

# ---------- 3.5. свежий xray-core ----------
# gozargah/marzban-node:latest тащит xray-core, вмороженный в образ на момент
# сборки — он отстаёт от того, что реально используют клиентские приложения
# (Happ, v2rayNG и т.п. часто следят за pre-release тегами XTLS). REALITY —
# протокол с меняющейся схемой (напр. post-quantum mldsa65-поля). Рассинхрон
# версий даёт классический симптом: клиент рвёт соединение с логом
#   "REALITY: received real certificate (potential MITM or redirection)"
# Ставим последний тег XTLS/Xray-core (включая pre-release) поверх того, что в
# образе. См. node/TROUBLESHOOTING.md.
echo; ok "Обновляю xray-core до актуальной версии (совместимость с клиентами)…"
XRAY_TARGET_VER="${XRAY_VERSION:-}"
XRAY_PIN_FILE="$(dirname "${BASH_SOURCE[0]:-$0}")/XRAY_VERSION"
if [ -z "$XRAY_TARGET_VER" ] && [ -f "$XRAY_PIN_FILE" ]; then
  XRAY_TARGET_VER="$(tr -d '[:space:]' < "$XRAY_PIN_FILE")"
  ok "Использую зафиксированную версию из node/XRAY_VERSION: $XRAY_TARGET_VER"
fi
if [ -z "$XRAY_TARGET_VER" ]; then
  warn "node/XRAY_VERSION не найден рядом со скриптом (запущен standalone-curl?) — беру GitHub latest."
  XRAY_TARGET_VER="$(curl -fsSL "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=5" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['tag_name'])" 2>/dev/null || true)"
fi
if [ -n "$XRAY_TARGET_VER" ]; then
  XTMP="$(mktemp -d)"
  if curl -fsSL --max-time 90 -o "$XTMP/xray.zip" \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_TARGET_VER}/Xray-linux-64.zip" 2>/dev/null; then
    apt-get install -y unzip -qq >/dev/null 2>&1 || true
    unzip -o "$XTMP/xray.zip" -d "$XTMP/x" >/dev/null 2>&1
    if [ -x "$XTMP/x/xray" ]; then
      docker cp "$XTMP/x/xray" marzban-node-marzban-node-1:/usr/local/bin/xray
      docker restart marzban-node-marzban-node-1 >/dev/null
      sleep 4
      XRAY_INSTALLED="$(docker exec marzban-node-marzban-node-1 xray version 2>/dev/null | head -1 || true)"
      ok "xray-core обновлён: ${XRAY_INSTALLED:-см. docker exec marzban-node-marzban-node-1 xray version}"
    else
      warn "Архив xray-core скачался, но бинарник не нашёлся внутри — оставляю версию из образа."
    fi
  else
    warn "Не смог скачать xray-core ${XRAY_TARGET_VER} — оставляю версию из образа marzban-node."
  fi
  rm -rf -- "$XTMP"
else
  warn "Не смог определить актуальную версию xray-core (GitHub недоступен?) — оставляю версию из образа."
fi
warn "Замена бинарника живёт в writable layer контейнера — переживёт restart/reboot, но НЕ 'docker compose up --force-recreate'."
warn "Для повторного апгрейда позже: node/upgrade_xray.sh на самой ноде."

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
  die "Внутренняя ошибка: IP панели не определён. UFW не будет открывать node-порты всем."
fi
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
ok "UFW включён."

# ---------- 4.5. fail2ban ----------
# SSH 22/tcp открыт всем (нода — расходный "скот", постоянного набора админских
# IP нет). Без fail2ban это голый root+password под открытым интернетом —
# брутфорс начинается в первые минуты после поднятия. UFW тут не спасает
# (он про порты, не про частоту попыток).
echo; ok "Ставлю fail2ban…"
apt-get install -y fail2ban -qq >/dev/null 2>&1 && systemctl enable --now fail2ban >/dev/null 2>&1 \
  && ok "fail2ban включён." \
  || warn "fail2ban не встал — поставь руками (apt-get install fail2ban)."

# ---------- 5. регистрация в панели ----------
echo; ok "Регистрирую ноду в панели…"
REG="$(curl -fsSL -X POST "$PANEL_URL/api/node" \
        --config "$CURL_AUTH_CONFIG" -H "Content-Type: application/json" \
        -d "{\"name\":\"${NODE_NAME}\",\"address\":\"${PUBIP}\",\"port\":${SERVICE_PORT},\"api_port\":${XRAY_API_PORT},\"add_as_new_host\":true}" || true)"
NODE_ID="$(echo "$REG" | jget id)"
if [ -n "$NODE_ID" ]; then
  ok "Нода зарегистрирована: id=$NODE_ID name=$NODE_NAME address=$PUBIP"
else
  warn "Авто-регистрация не удалась (возможно, нода с таким адресом уже есть). Ответ: ${REG:0:200}"
  warn "Зарегистрируй руками в панели: Node Settings → Add Node → address=$PUBIP, port=$SERVICE_PORT, api_port=$XRAY_API_PORT."
fi

# ---------- 6. честная проверка туннеля ----------
# "connected" в панели — это здоровье node-API (62050/62051), НЕ auth самого
# REALITY-инбаунда. Создаём одноразового юзера, гоняем через него настоящий
# аутентифицированный туннель прямо здесь, на ноде, и удаляем юзера. Ловит
# сломанный/отсутствующий инбаунд, version skew и т.п. сразу при разворачивании,
# а не через день жалоб от живых клиентов. См. TROUBLESHOOTING.md.
echo; ok "Проверяю туннель по-настоящему (не просто 'connected')…"
_write_curl_auth
VERIFY_JSON="$(mktemp)"
python3 - "$PANEL_URL" "$TOKEN" > "$VERIFY_JSON" <<'PY'
import sys, urllib.request, json, secrets, uuid
from urllib.parse import urlparse, parse_qsl
base, token = sys.argv[1], sys.argv[2]
ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("User-Agent", ua); req.add_header("Authorization", "Bearer " + token)
    if data is not None: req.add_header("Content-Type", "application/json")
    r = urllib.request.urlopen(req, timeout=30); raw = r.read()
    return json.loads(raw) if raw else {}
cfg = api("GET", "/api/core/config")
tags = [i["tag"] for i in cfg.get("inbounds", []) if i.get("streamSettings", {}).get("security") == "reality"]
if not tags:
    print(json.dumps({"skip": "no reality inbound configured yet"})); raise SystemExit(0)
tag = tags[0]
username = "verify-" + secrets.token_hex(4)
client_id = str(uuid.uuid4())
api("POST", "/api/user", {"username": username, "status": "active",
    "proxies": {"vless": {"id": client_id, "flow": "xtls-rprx-vision"}},
    "inbounds": {"vless": [tag]}})
u = api("GET", f"/api/user/{username}")
p = urlparse(u["links"][0]); q = dict(parse_qsl(p.query))
print(json.dumps({"username": username, "uuid": p.username, "pbk": q.get("pbk"),
    "sid": q.get("sid"), "sni": q.get("sni"), "fp": q.get("fp", "chrome")}))
PY

SKIP="$(python3 -c "import json;print(json.load(open('$VERIFY_JSON')).get('skip',''))")"
if [ -n "$SKIP" ]; then
  warn "Проверка пропущена: $SKIP"
  warn "Опиши REALITY-инбаунд в панели (Core Config), потом прогони node/verify_node.sh."
else
  VUSER="$(python3 -c "import json;print(json.load(open('$VERIFY_JSON'))['username'])")"
  VCFG="$(mktemp)"
  python3 - "$VERIFY_JSON" "$PUBIP" > "$VCFG" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); node_ip = sys.argv[2]
cfg = {"log": {"loglevel": "warning"},
  "inbounds": [{"port": 19999, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}}],
  "outbounds": [{"protocol": "vless",
    "settings": {"vnext": [{"address": node_ip, "port": 443, "users": [
      {"id": d["uuid"], "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
    "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
      "serverName": d["sni"], "fingerprint": d["fp"], "publicKey": d["pbk"],
      "shortId": d["sid"], "spiderX": "/"}}}]}
json.dump(cfg, open("/dev/stdout", "w"))
PY
  cp "$VCFG" /var/lib/marzban-node/verify_client.json
  ( cd /opt/marzban-node && docker compose exec -d marzban-node xray run -config /var/lib/marzban-node/verify_client.json )
  sleep 3
  VRESULT="$(curl --socks5-hostname 127.0.0.1:19999 -s -o /dev/null -w '%{http_code}' --max-time 10 https://www.gstatic.com/generate_204 || true)"
  VPID="$(ss -tlnp 2>/dev/null | grep '127.0.0.1:19999' | grep -oE 'pid=[0-9]+' | cut -d= -f2)"
  [ -n "$VPID" ] && kill "$VPID" 2>/dev/null || true
  rm -f /var/lib/marzban-node/verify_client.json "$VCFG"
  _write_curl_auth
  curl -fsSL -X DELETE "$PANEL_URL/api/user/$VUSER" --config "$CURL_AUTH_CONFIG" >/dev/null 2>&1 || true
  if [ "$VRESULT" = "204" ]; then
    ok "ТУННЕЛЬ ПРОВЕРЕН: реальный клиент прошёл REALITY-хендшейк и получил трафик."
  else
    warn "ТУННЕЛЬ НЕ ПРОШЁЛ (ответ: ${VRESULT:-нет ответа}). Нода connected, но клиенты работать НЕ будут."
    warn "Смотри node/TROUBLESHOOTING.md — начни с версии xray-core."
  fi
fi
rm -f "$VERIFY_JSON"

echo
echo -e "${G}ГОТОВО.${NC}"
echo "xray-core на ноде: ${XRAY_INSTALLED:-версия из образа marzban-node, см. warn выше}"
echo "Проверь в панели: нода должна стать 'connected' за ~10-30 сек."
echo "Логи ноды:  cd /opt/marzban-node && docker compose logs -f"
echo "Далее: опиши REALITY-инбаунд в САМОЙ панели (Core Config / Hosts) — нода его подхватит,"
echo "а add_as_new_host уже добавил адрес $PUBIP в Hosts, так что ссылки поедут на эту ноду."
echo
echo "Если у живых клиентов REALITY не пингуется ('received real certificate' в логе клиента) —"
echo "это почти всегда рассинхрон версий xray-core клиент/сервер. См. node/TROUBLESHOOTING.md."
