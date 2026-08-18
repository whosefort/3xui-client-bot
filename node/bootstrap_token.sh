#!/usr/bin/env bash
#
# bootstrap_token.sh — версия bootstrap.sh для развёртывания через бота
# («🖥 Добавить сервер» в админке). В отличие от bootstrap.sh:
#   - НЕ спрашивает логин/пароль панели — их тут вообще нет;
#   - НЕ регистрирует ноду в Marzban — бот уже сделал это до выдачи токена;
#   - cert и IP панели забирает у бота по одноразовому токену.
#
# Запуск (команду целиком даёт бот):
#   curl -fsSL .../bootstrap_token.sh | NODE_TOKEN=xxx CLAIM_URL=https://... bash
#
# После разворачивания честную проверку туннеля гоняй отдельно:
#   node/verify_node.sh (с панельскими кредами, с любой машины по SSH)
# — сам bootstrap_token.sh их не имеет и специально не может.

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; NC='\033[0m'
ok(){ echo -e "${G}✓${NC} $*"; }; warn(){ echo -e "${Y}⚠${NC}  $*"; }
die(){ echo -e "${R}✗${NC} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запускай под root."
command -v apt-get >/dev/null 2>&1 || die "Только Debian/Ubuntu (apt)."

NODE_TOKEN="${NODE_TOKEN:?задай NODE_TOKEN (его даёт бот)}"
CLAIM_URL="${CLAIM_URL:?задай CLAIM_URL (его даёт бот)}"
SERVICE_PORT="${SERVICE_PORT:-62050}"
XRAY_API_PORT="${XRAY_API_PORT:-62051}"
INBOUND_PORTS="${INBOUND_PORTS:-443 8443}"

# ---------- 0. забираем cert + IP панели у бота ----------
echo; ok "Забираю данные ноды у бота…"
jget(){ python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

CLAIM_JSON="$(mktemp)"
trap 'rm -f -- "$CLAIM_JSON"' EXIT
HTTP_CODE="$(curl -sS -o "$CLAIM_JSON" -w '%{http_code}' -X POST "$CLAIM_URL" \
              -H 'Content-Type: application/json' \
              -d "{\"token\":\"${NODE_TOKEN}\"}" || true)"
if [ "$HTTP_CODE" != "200" ]; then
  ERR="$(jget error < "$CLAIM_JSON" 2>/dev/null || true)"
  die "Не забрал данные у бота (HTTP $HTTP_CODE): ${ERR:-см. CLAIM_URL/NODE_TOKEN}. Токен одноразовый и живёт недолго — попроси у бота новый."
fi

NODE_ID="$(jget node_id < "$CLAIM_JSON")"
REG_ADDR="$(jget address < "$CLAIM_JSON")"
PANEL_IP="$(jget panel_ip < "$CLAIM_JSON")"
XRAY_TARGET_VER="$(jget xray_version < "$CLAIM_JSON")"
NODE_IMAGE_PINNED="$(jget node_image < "$CLAIM_JSON")"
BOT_SSH_PUBKEY="$(jget bot_ssh_pubkey < "$CLAIM_JSON")"
python3 -c "import sys,json; print(json.load(open('$CLAIM_JSON')).get('cert_pem',''))" > /tmp/_claimed_cert.pem
[ -s /tmp/_claimed_cert.pem ] || die "Пустой cert от бота."
head -1 /tmp/_claimed_cert.pem | grep -q "BEGIN CERT" || die "cert от бота не похож на PEM."
ok "Нода id=$NODE_ID уже зарегистрирована в панели (address=$REG_ADDR)."

# Ключ бота — только для удалённого node/upgrade_xray.sh («Обновить xray на
# нодах» в админке), больше ничего им не делается.
if [ -n "$BOT_SSH_PUBKEY" ]; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
  grep -qxF "$BOT_SSH_PUBKEY" /root/.ssh/authorized_keys || echo "$BOT_SSH_PUBKEY" >> /root/.ssh/authorized_keys
  ok "SSH-ключ бота добавлен в authorized_keys (для удалённого апгрейда xray-core)."
fi

python3 -c "import ipaddress; ipaddress.ip_address('$PANEL_IP')" 2>/dev/null \
  || die "Бот вернул некорректный panel_ip ($PANEL_IP) — баг на стороне бота."

# ---------- публичный IP этой машины (сверка с тем, что регистрировал админ) ----------
PUBIP="$(curl -fsSL https://api.ipify.org 2>/dev/null || true)"
[ -n "$PUBIP" ] || PUBIP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
if [ -n "$PUBIP" ] && [ -n "$REG_ADDR" ] && [ "$PUBIP" != "$REG_ADDR" ]; then
  warn "Публичный IP этой машины ($PUBIP) не совпадает с тем, что вводили в боте ($REG_ADDR)."
  warn "Если это NAT/несколько адресов — ок. Если нет — ссылки клиентов поедут не туда, проверь потом Hosts в панели."
fi

# ---------- 1. память: swap + отключение мусорных демонов ----------
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

# ---------- 2. Docker ----------
echo; ok "Ставлю Docker…"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || die "docker compose v2 не найден."
ok "Docker готов."

# ---------- 3. cert на место ----------
mkdir -p /var/lib/marzban-node
cp /tmp/_claimed_cert.pem /var/lib/marzban-node/ssl_client_cert.pem
chmod 600 /var/lib/marzban-node/ssl_client_cert.pem
rm -f /tmp/_claimed_cert.pem
ok "Cert панели сохранён."

# ---------- 4. marzban-node (docker) ----------
# Образ marzban-node: по умолчанию зафиксированная версия, которую бот отдал
# в claim-ответе (node_image, читает node/MARZBAN_NODE_IMAGE — тот же паттерн,
# что xray_version). Пайп через curl не умеет спрашивать интерактивно,
# поэтому выбор канала — через env var в самой команде:
#   ... | NODE_TOKEN=x CLAIM_URL=y NODE_IMAGE_CHANNEL=latest bash
NODE_IMAGE_CHANNEL="${NODE_IMAGE_CHANNEL:-stable}"
if [ "$NODE_IMAGE_CHANNEL" = "stable" ] && [ -n "$NODE_IMAGE_PINNED" ]; then
  NODE_IMAGE="$NODE_IMAGE_PINNED"
  ok "Образ marzban-node: зафиксированная версия ($NODE_IMAGE)"
else
  [ "$NODE_IMAGE_CHANNEL" = "stable" ] && warn "Бот не прислал зафиксированную версию — беру :latest."
  NODE_IMAGE="gozargah/marzban-node:latest"
  warn "Образ marzban-node: :latest — версия не зафиксирована."
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

# ---------- 4.5. свежий xray-core (версия, которую отдал бот) ----------
# См. node/TROUBLESHOOTING.md #1: клиенты обновляют xray-core сами, образ
# marzban-node — нет. Рассинхрон рвёт REALITY-хендшейк молча.
echo; ok "Ставлю xray-core ${XRAY_TARGET_VER:-(версия не пришла от бота, беру GitHub latest)}…"
if [ -z "$XRAY_TARGET_VER" ]; then
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
      ok "xray-core: ${XRAY_INSTALLED:-см. docker exec marzban-node-marzban-node-1 xray version}"
    else
      warn "Архив xray-core скачался, но бинарник не нашёлся внутри — оставляю версию из образа."
    fi
  else
    warn "Не смог скачать xray-core ${XRAY_TARGET_VER} — оставляю версию из образа marzban-node."
  fi
  rm -rf -- "$XTMP"
else
  warn "Не смог определить версию xray-core — оставляю версию из образа."
fi
warn "Патч бинарника живёт в writable layer контейнера — переживёт restart/reboot, не 'force-recreate'."
warn "Повторный апгрейд позже: node/upgrade_xray.sh."

# ---------- 5. firewall ----------
echo; ok "Настраиваю UFW…"
apt-get install -y ufw >/dev/null 2>&1 || true
ufw allow 22/tcp comment 'SSH' >/dev/null
for p in $INBOUND_PORTS; do ufw allow "${p}/tcp" comment 'inbound' >/dev/null; done
ufw allow proto tcp from "$PANEL_IP" to any port "$SERVICE_PORT" comment 'panel-node' >/dev/null
ufw allow proto tcp from "$PANEL_IP" to any port "$XRAY_API_PORT" comment 'panel-xray' >/dev/null
ok "node-порты $SERVICE_PORT/$XRAY_API_PORT открыты только для панели ($PANEL_IP)"
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
ok "UFW включён."

# ---------- 5.5. fail2ban ----------
# SSH 22/tcp открыт всем — без fail2ban это голый root+password под открытым
# интернетом. UFW тут не спасает, он про порты, не про частоту попыток.
echo; ok "Ставлю fail2ban…"
apt-get install -y fail2ban -qq >/dev/null 2>&1 && systemctl enable --now fail2ban >/dev/null 2>&1 \
  && ok "fail2ban включён." \
  || warn "fail2ban не встал — поставь руками (apt-get install fail2ban)."

echo
echo -e "${G}ГОТОВО.${NC}"
echo "Нода id=$NODE_ID должна стать 'connected' в панели за ~10-30 сек."
echo "Логи ноды:  cd /opt/marzban-node && docker compose logs -f"
echo
echo "Регистрацию и REALITY-инбаунд бот/панель уже знают — новых ссылок для этой"
echo "ноды пока нет, если для её тега инбаунда ещё не настроен Host. Обычно всё"
echo "уже готово (host скопирован при регистрации), но проверь в панели."
echo
echo "⚠️ Честную проверку туннеля (не 'connected', а реальный REALITY-хендшейк)"
echo "у этого скрипта нет — он специально не имеет паролей от панели. Попроси"
echo "прогнать: node/verify_node.sh NODE_IP=$PUBIP (с машины, где есть креды панели)."
