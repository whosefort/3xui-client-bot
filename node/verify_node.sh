#!/usr/bin/env bash
#
# verify_node.sh — честная проверка ноды: создаёт одноразового тестового
# юзера в панели, гоняет через него реальный REALITY-туннель ПРЯМО С НОДЫ на
# её публичный IP, проверяет, что трафик реально ходит — и удаляет юзера.
#
# Зачем: "нода connected в панели" НЕ значит "клиент сможет подключиться".
# Panel-статус connected — это здоровье node-API (62050/62051), а не auth
# самого REALITY-инбаунда. Ровно так был потерян день на инцидент, который
# эта проверка ловит за 10 секунд. См. TROUBLESHOOTING.md.
#
# Запуск (с любой машины с доступом до панели и SSH до ноды):
#   PANEL_URL=https://mon.example.com PANEL_USER=admin PANEL_PASS=*** \
#   NODE_IP=1.2.3.4 NODE_SSH_PASS=*** bash verify_node.sh
#
# Если у ноды ещё нет ни одного REALITY-инбаунда в панели (самый первый нода
# в жизни панели) — скрипт это скажет и завершится с warning, не с ошибкой:
# сначала нужно один раз описать инбаунд в Core Config (см. README.md).

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; NC='\033[0m'
ok(){ echo -e "${G}✓${NC} $*"; }; warn(){ echo -e "${Y}⚠${NC}  $*"; }
die(){ echo -e "${R}✗${NC} $*" >&2; exit 1; }

PANEL_URL="${PANEL_URL:?задай PANEL_URL}"; PANEL_URL="${PANEL_URL%/}"
PANEL_USER="${PANEL_USER:?задай PANEL_USER}"
PANEL_PASS="${PANEL_PASS:?задай PANEL_PASS}"
NODE_IP="${NODE_IP:?задай NODE_IP — публичный адрес ноды, которую проверяем}"
NODE_SSH_USER="${NODE_SSH_USER:-root}"
NODE_SSH_PASS="${NODE_SSH_PASS:-}"
CONTAINER="${CONTAINER:-marzban-node-marzban-node-1}"

command -v sshpass >/dev/null 2>&1 || die "нужен sshpass (или запусти вручную по логике ниже с обычным ssh)."
[ -n "$NODE_SSH_PASS" ] || die "задай NODE_SSH_PASS (или поправь SSH-вызовы на ключевую авторизацию)."

WORKDIR="$(mktemp -d)"
trap 'rm -rf -- "$WORKDIR"' EXIT

echo "=== 1. логинюсь в панель ==="
TOKEN="$(python3 - "$PANEL_URL" "$PANEL_USER" "$PANEL_PASS" <<'PY'
import sys, urllib.request, urllib.parse, json
base, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
d = urllib.parse.urlencode({"username": user, "password": pw}).encode()
r = urllib.request.Request(base + "/api/admin/token", data=d, method="POST")
r.add_header("User-Agent", ua)
print(json.load(urllib.request.urlopen(r, timeout=30))["access_token"])
PY
)"
[ -n "$TOKEN" ] || die "Не залогинился в панель."
ok "токен получен"

echo "=== 2. ищу REALITY-инбаунд + создаю одноразового юзера ==="
VERIFY_JSON="$WORKDIR/verify.json"
python3 - "$PANEL_URL" "$TOKEN" > "$VERIFY_JSON" <<'PY'
import sys, urllib.request, json, secrets, uuid

base, token = sys.argv[1], sys.argv[2]
ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("User-Agent", ua)
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    r = urllib.request.urlopen(req, timeout=30)
    raw = r.read()
    return json.loads(raw) if raw else {}

cfg = api("GET", "/api/core/config")
reality_tags = [
    i["tag"] for i in cfg.get("inbounds", [])
    if i.get("streamSettings", {}).get("security") == "reality"
]
if not reality_tags:
    print(json.dumps({"skip": "no reality inbound configured yet"}))
    raise SystemExit(0)
tag = reality_tags[0]

username = "verify-" + secrets.token_hex(4)
client_id = str(uuid.uuid4())
api("POST", "/api/user", {
    "username": username,
    "status": "active",
    "proxies": {"vless": {"id": client_id, "flow": "xtls-rprx-vision"}},
    "inbounds": {"vless": [tag]},
})
u = api("GET", f"/api/user/{username}")
link = u["links"][0]

from urllib.parse import urlparse, parse_qsl
p = urlparse(link)
q = dict(parse_qsl(p.query))
print(json.dumps({
    "username": username, "tag": tag,
    "uuid": p.username, "pbk": q.get("pbk"), "sid": q.get("sid"),
    "sni": q.get("sni"), "fp": q.get("fp", "chrome"),
}))
PY

SKIP="$(python3 -c "import json;d=json.load(open('$VERIFY_JSON'));print(d.get('skip',''))")"
if [ -n "$SKIP" ]; then
  warn "Пропускаю проверку: $SKIP"
  warn "Опиши REALITY-инбаунд в панели (Core Config) и прогони скрипт ещё раз."
  exit 0
fi

USERNAME="$(python3 -c "import json;print(json.load(open('$VERIFY_JSON'))['username'])")"
cleanup_user() {
  python3 - "$PANEL_URL" "$TOKEN" "$USERNAME" <<'PY'
import sys, urllib.request
base, token, username = sys.argv[1], sys.argv[2], sys.argv[3]
ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
req = urllib.request.Request(base + f"/api/user/{username}", method="DELETE")
req.add_header("User-Agent", ua)
req.add_header("Authorization", "Bearer " + token)
urllib.request.urlopen(req, timeout=15)
PY
}
# любая CDN (Cloudflare и т.п.) перед панелью режет запросы без browser-like
# User-Agent 403-м — забыть заголовок в ОДНОМ из вызовов легко и незаметно,
# если ошибка проглочена. Поэтому здесь без "|| true": если удаление не
# прошло, увидим это явно, а не тихо оставим мусорного юзера в панели.
trap 'cleanup_user; rm -rf -- "$WORKDIR"' EXIT
ok "тестовый юзер $USERNAME создан на инбаунде $(python3 -c "import json;print(json.load(open('$VERIFY_JSON'))['tag'])")"

echo "=== 3. собираю клиентский конфиг ==="
python3 - "$VERIFY_JSON" "$NODE_IP" > "$WORKDIR/client.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
node_ip = sys.argv[2]
cfg = {
    "log": {"loglevel": "warning"},
    "inbounds": [{"port": 19999, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}}],
    "outbounds": [{
        "protocol": "vless",
        "settings": {"vnext": [{"address": node_ip, "port": 443, "users": [
            {"id": d["uuid"], "encryption": "none", "flow": "xtls-rprx-vision"}
        ]}]},
        "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
            "serverName": d["sni"], "fingerprint": d["fp"],
            "publicKey": d["pbk"], "shortId": d["sid"], "spiderX": "/",
        }},
    }],
}
json.dump(cfg, open("/dev/stdout", "w"))
PY

echo "=== 4. гоняю туннель прямо с ноды ==="
# marzban-node — минимальный образ: внутри нет curl/wget/pkill/pgrep. Поэтому:
# - xray-клиент стартуем в контейнере через 'exec -d' (штатный detach, без
#   ручного backgrounding);
# - curl гоняем СНАРУЖИ, с хоста ноды — network_mode: host расшаривает сетевой
#   неймспейс контейнера, сокет 127.0.0.1:19999 виден и достижим напрямую;
# - на ноде в этом же контейнере всегда крутится СВОЙ основной xray
#   (config stdin: — реально держит 443 для живых клиентов) — его нельзя
#   трогать. Поэтому глушим тестовый процесс не по имени (xray/pkill), а по
#   PID, который ss резолвит именно для нашего уникального порта 19999 —
#   Linux-неймспейсы вложены, хостовый ss корректно видит и адресует
#   процессы контейнера через network_mode: host.
REMOTE_CFG="/var/lib/marzban-node/verify_client_$$.json"
sshpass -p "$NODE_SSH_PASS" scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
  "$WORKDIR/client.json" "$NODE_SSH_USER@$NODE_IP:$REMOTE_CFG" >/dev/null

RESULT="$(sshpass -p "$NODE_SSH_PASS" ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$NODE_SSH_USER@$NODE_IP" "
  cd /opt/marzban-node
  docker compose exec -d marzban-node xray run -config '$REMOTE_CFG'
  sleep 3
  curl --socks5-hostname 127.0.0.1:19999 -s -o /dev/null -w '%{http_code}' --max-time 10 https://www.gstatic.com/generate_204
  PID=\$(ss -tlnp 2>/dev/null | grep '127.0.0.1:19999' | grep -oE 'pid=[0-9]+' | cut -d= -f2)
  [ -n \"\$PID\" ] && kill \"\$PID\" 2>/dev/null
  rm -f '$REMOTE_CFG'
")"

if [ "$RESULT" = "204" ]; then
  ok "ТУННЕЛЬ РАБОТАЕТ (http=204). Нода $NODE_IP готова к реальным клиентам."
  exit 0
else
  die "ТУННЕЛЬ НЕ РАБОТАЕТ (ответ: $RESULT). Смотри TROUBLESHOOTING.md — начни с версии xray-core."
fi
