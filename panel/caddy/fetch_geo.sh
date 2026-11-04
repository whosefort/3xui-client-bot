#!/usr/bin/env bash
# Скачивает geoip.dat/geosite.dat в /var/www/happ-geo для раздачи Caddy'ем
# с sub-домена (блок handle_path /geo/* в Caddyfile.tmpl). Версии — пины из
# panel/happ_routing.deeplink (поля Geoipurl/Geositeurl базового профиля),
# т.е. ровно те, что бот раздаёт клиентам. sha256 сверяется с апстримным
# .sha256, замена файлов атомарная. Идемпотентный — можно в cron.
#
# Ручной запуск на мастере:   bash panel/caddy/fetch_geo.sh
# Cron (еженедельно):         23 4 * * 0  bash /root/3xui-client-bot/panel/caddy/fetch_geo.sh >> /var/log/happ-geo-fetch.log 2>&1
set -euo pipefail

DEST="${GEO_DIR:-/var/www/happ-geo}"
DEEPLINK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/happ_routing.deeplink"

# Достаём Geoipurl/Geositeurl из JSON внутри happ://routing/onadd/<base64>.
read -r GEOIP_URL GEOSITE_URL < <(
  python3 - "$DEEPLINK" <<'PY'
import base64, json, sys
raw = open(sys.argv[1]).read().strip()
b64 = raw.split("happ://routing/onadd/", 1)[1]
d = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
print(d["Geoipurl"], d["Geositeurl"])
PY
)

mkdir -p "$DEST"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch() { # $1=url $2=имя файла
  curl -fsSL --max-time 120 "$1" -o "$tmp/$2"
  curl -fsSL --max-time 30 "$1.sha256" -o "$tmp/$2.sha256"
  want="$(awk '{print $1}' "$tmp/$2.sha256")"
  got="$(sha256sum "$tmp/$2" | awk '{print $1}')"
  if [ "$want" != "$got" ]; then
    echo "ОШИБКА: sha256 не сошёлся для $2 ($got != $want)" >&2
    exit 1
  fi
}

fetch "$GEOIP_URL" geoip.dat
fetch "$GEOSITE_URL" geosite.dat

changed=0
for f in geoip.dat geosite.dat; do
  if ! cmp -s "$tmp/$f" "$DEST/$f" 2>/dev/null; then
    install -m 0644 "$tmp/$f" "$DEST/$f.new" && mv "$DEST/$f.new" "$DEST/$f"
    changed=1
  fi
done

if [ "$changed" = 1 ]; then
  echo "$(date -Is) обновлено: $(ls -l "$DEST" | awk 'NR>1{print $9"("$5")"}' | paste -sd' ' -)"
else
  echo "$(date -Is) без изменений (уже актуальные)"
fi
