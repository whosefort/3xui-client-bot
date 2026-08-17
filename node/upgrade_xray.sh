#!/usr/bin/env bash
#
# upgrade_xray.sh — ставит свежий xray-core внутрь уже запущенного marzban-node.
#
# Зачем: gozargah/marzban-node:latest тащит xray-core, вмороженный в образ на
# момент сборки — он быстро отстаёт от того, что реально используют клиентские
# приложения (Happ, v2rayNG и т.п. часто следят за pre-release тегами XTLS).
# REALITY — протокол с меняющейся схемой (напр. добавление post-quantum
# mldsa65-полей). Рассинхрон версий клиент/сервер даёт классический симптом:
# клиент рвёт соединение с логом
#   "REALITY: received real certificate (potential MITM or redirection)"
# — сервер не распознал auth нового формата и откатился на honest-relay к dest.
# См. node/TROUBLESHOOTING.md.
#
# Запуск (на самой ноде, под root):
#   bash upgrade_xray.sh                  # берёт самый свежий тег XTLS/Xray-core
#   XRAY_VERSION=v26.7.11 bash upgrade_xray.sh   # пин конкретной версии
#
# Идемпотентно — можно гонять по крону/после каждого bootstrap.

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; NC='\033[0m'
ok(){ echo -e "${G}✓${NC} $*"; }; warn(){ echo -e "${Y}⚠${NC}  $*"; }
die(){ echo -e "${R}✗${NC} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запускай под root."

CONTAINER="${CONTAINER:-marzban-node-marzban-node-1}"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "Контейнер $CONTAINER не найден. Задай CONTAINER=имя, если у тебя другое (docker ps)."

XRAY_VERSION="${XRAY_VERSION:-}"
if [ -z "$XRAY_VERSION" ]; then
  echo "Определяю последний тег XTLS/Xray-core (включая pre-release — именно на них живут клиенты)…"
  XRAY_VERSION="$(curl -fsSL "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=5" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['tag_name'])")"
fi
[ -n "$XRAY_VERSION" ] || die "Не смог определить версию xray-core. Задай вручную: XRAY_VERSION=v26.7.11"
ok "Целевая версия: $XRAY_VERSION"

CUR="$(docker exec "$CONTAINER" xray version 2>/dev/null | head -1 || true)"
echo "Сейчас в контейнере: ${CUR:-неизвестно}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf -- "$TMPDIR"' EXIT
URL="https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip"
echo "Качаю $URL …"
curl -fsSL --max-time 90 -o "$TMPDIR/xray.zip" "$URL" || die "Скачать не удалось — версия $XRAY_VERSION существует? (проверь https://github.com/XTLS/Xray-core/releases)"

command -v unzip >/dev/null 2>&1 || apt-get install -y unzip -qq >/dev/null 2>&1
unzip -o "$TMPDIR/xray.zip" -d "$TMPDIR/extracted" >/dev/null

[ -x "$TMPDIR/extracted/xray" ] || die "В архиве нет исполняемого xray — сборка релиза изменилась?"
NEWVER="$("$TMPDIR/extracted/xray" version | head -1)"
ok "Скачано: $NEWVER"

docker cp "$TMPDIR/extracted/xray" "$CONTAINER:/usr/local/bin/xray"
docker restart "$CONTAINER" >/dev/null
sleep 4

INVER="$(docker exec "$CONTAINER" xray version 2>/dev/null | head -1 || true)"
[ "$INVER" = "$NEWVER" ] || die "После рестарта версия в контейнере не совпадает с установленной ($INVER). Смотри docker logs $CONTAINER."
ok "В контейнере теперь: $INVER"

if docker exec "$CONTAINER" sh -c 'command -v ss >/dev/null 2>&1 && ss -tlnp 2>/dev/null | grep -q xray' \
   || ss -tlnp 2>/dev/null | grep -q 'users:(("xray"'; then
  ok "xray слушает — процесс жив после апгрейда."
else
  warn "Не вижу xray среди слушающих сокетов — проверь docker logs $CONTAINER и core config в панели."
fi

echo
echo -e "${G}Готово.${NC} Не забудь: замена бинарника живёт в writable layer контейнера —"
echo "переживёт restart/reboot, но НЕ переживёт 'docker compose up --force-recreate' или пересборку образа."
echo "Перезапускай upgrade_xray.sh после любого пересоздания контейнера ноды."
