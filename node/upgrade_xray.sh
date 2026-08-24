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

# Релизы XTLS/Xray-core публикуют .dgst рядом с каждым zip — сверяем sha256,
# иначе подмена бинарника (взломанный релиз/угнанный GH Actions токен/hijack
# редиректа) молча ставится вместо честного xray-core, а это процесс,
# терминирующий живой REALITY-трафик всех клиентов ноды. Если .dgst недоступен
# (сеть/смена формата релиза) — не блокируем апгрейд, только предупреждаем.
_verify_xray_zip(){
  local zip="$1" url="$2" dgst expected actual
  dgst="$(curl -fsSL --max-time 20 "${url}.dgst" 2>/dev/null || true)"
  if [ -z "$dgst" ]; then
    warn "Релиз без .dgst — качаю без проверки контрольной суммы."
    return 0
  fi
  expected="$(echo "$dgst" | awk '/SHA2-256/{print $2}')"
  if [ -z "$expected" ]; then
    warn "Не смог разобрать .dgst — качаю без проверки контрольной суммы."
    return 0
  fi
  actual="$(sha256sum "$zip" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || die "Контрольная сумма xray.zip не совпадает с релизом XTLS (ожидали $expected, получили $actual) — возможна подмена бинарника."
  ok "Контрольная сумма xray.zip подтверждена (sha256)."
}

[ "$(id -u)" -eq 0 ] || die "Запускай под root."

CONTAINER="${CONTAINER:-marzban-node-marzban-node-1}"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "Контейнер $CONTAINER не найден. Задай CONTAINER=имя, если у тебя другое (docker ps)."

XRAY_VERSION="${XRAY_VERSION:-}"
XRAY_PIN_FILE="$(dirname "${BASH_SOURCE[0]:-$0}")/XRAY_VERSION"
if [ -z "$XRAY_VERSION" ] && [ -f "$XRAY_PIN_FILE" ]; then
  XRAY_VERSION="$(tr -d '[:space:]' < "$XRAY_PIN_FILE")"
  echo "Использую зафиксированную версию из node/XRAY_VERSION: $XRAY_VERSION"
fi
if [ -z "$XRAY_VERSION" ]; then
  echo "node/XRAY_VERSION не найден — беру последний тег XTLS/Xray-core с GitHub (включая pre-release)…"
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
_verify_xray_zip "$TMPDIR/xray.zip" "$URL"

command -v unzip >/dev/null 2>&1 || apt-get install -y unzip -qq >/dev/null 2>&1
unzip -o "$TMPDIR/xray.zip" -d "$TMPDIR/extracted" >/dev/null

[ -x "$TMPDIR/extracted/xray" ] || die "В архиве нет исполняемого xray — сборка релиза изменилась?"
NEWVER="$("$TMPDIR/extracted/xray" version | head -1 || true)"
[ -n "$NEWVER" ] || die "Не смог прочитать версию из скачанного бинарника — архив побился?"
ok "Скачано: $NEWVER"

docker cp "$TMPDIR/extracted/xray" "$CONTAINER:/usr/local/bin/xray"
docker restart "$CONTAINER" >/dev/null
sleep 4

INVER="$(docker exec "$CONTAINER" xray version 2>/dev/null | head -1 || true)"
[ "$INVER" = "$NEWVER" ] || die "После рестарта версия в контейнере не совпадает с установленной ($INVER). Смотри docker logs $CONTAINER."
ok "В контейнере теперь: $INVER"

# После docker restart контейнеру нужно время поднять supervisor, законнектиться
# к панели и заспавнить xray заново — 4 сек на это не всегда хватает. В образе
# marzban-node нет ss/netstat вообще (минимальный образ), поэтому смотрим с
# ХОСТА — network_mode: host расшаривает сокеты напрямую. Ретраим вместо
# одного разового замера, чтобы не пугать ложным warning на медленном старте.
LISTENING=""
for _ in 1 2 3 4 5 6; do
  if ss -tlnp 2>/dev/null | grep -q 'users:(("xray"'; then
    LISTENING=1; break
  fi
  sleep 2
done
if [ -n "$LISTENING" ]; then
  ok "xray слушает — процесс жив после апгрейда."
else
  warn "За 16 сек не увидел xray среди слушающих сокетов хоста — проверь docker logs $CONTAINER и core config в панели."
fi

echo
echo -e "${G}Готово.${NC} Не забудь: замена бинарника живёт в writable layer контейнера —"
echo "переживёт restart/reboot, но НЕ переживёт 'docker compose up --force-recreate' или пересборку образа."
echo "Перезапускай upgrade_xray.sh после любого пересоздания контейнера ноды."
