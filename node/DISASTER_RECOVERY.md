# Нода умерла — разворот новой с нуля

Пошагово, без предположений о контексте. Годится и для планового расширения
(добавить ещё одну ноду), и для замены упавшей.

## 0. Что нужно под рукой

- Root-доступ к чистому VPS (Ubuntu/Debian) — новый сервер.
- `PANEL_URL`, `PANEL_USER`, `PANEL_PASS` — доступ к главной панели Marzban.
- Если панель за Cloudflare/CDN — реальный origin-IP панели (`PANEL_IP`).
  DNS-имя панели вернёт IP CDN, а не origin — соединения нода→панель придут
  именно с origin-IP, и `bootstrap.sh` откажется работать без него (осознанно
  — иначе node-порты пришлось бы открывать всем, а не только панели).
- Клон этого репозитория на новом VPS (`git clone ...`) — тогда `bootstrap.sh`
  сам подхватит `node/XRAY_VERSION` (зафиксированная, проверенная версия
  xray-core) и `node/verify_node.sh`/`upgrade_xray.sh` будут рядом.

## 1. Если старая нода ещё числится в панели — убрать её

Мёртвая нода в списке путает: панель может пытаться слать ей трафик/health-
check, а клиенты со старыми ссылками будут стучаться в дохлый IP.

```bash
# узнать id мёртвой ноды
curl -s -X POST "$PANEL_URL/api/admin/token" \
  -H "User-Agent: Mozilla/5.0" \
  --data-urlencode "username=$PANEL_USER" --data-urlencode "password=$PANEL_PASS" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])" > /tmp/tok

TOKEN=$(cat /tmp/tok)
curl -s "$PANEL_URL/api/nodes" -H "User-Agent: Mozilla/5.0" -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool

# удалить по id (смотри вывод выше)
curl -s -X DELETE "$PANEL_URL/api/node/<ID>" -H "User-Agent: Mozilla/5.0" -H "Authorization: Bearer $TOKEN"
```

Не забудь `User-Agent` в ручных curl-запросах к панели — если она за
Cloudflare, запрос без него молча схлопочет 403.

## 2. Купить/поднять новый VPS

Тот же хостер и тот же регион, что у остальных живых нод — если весь парк
уже провалидирован под конкретную сеть/DPI-профиль клиентов, менять регион
наугад = заново словить всё то, что описано в `TROUBLESHOOTING.md`. Меняй
регион/хостера осознанно, с последующей проверкой шагом 4, не по умолчанию.

RAM — от 1GB хватает (`bootstrap.sh` сам поднимет 2G swap на слабых VPS), но
2GB+ спокойнее.

## 3. Развернуть

```bash
git clone <URL_ЭТОГО_РЕПО> && cd <репо>/node
PANEL_URL=https://mon.your-domain.tld \
PANEL_USER=admin \
PANEL_PASS='...' \
PANEL_IP=<origin-IP панели, если за CDN> \
NODE_NAME=<осмысленное имя, напр. eu-frankfurt-02> \
bash bootstrap.sh
```

Скрипт сам:
1. Поставит Docker, подготовит swap.
2. Поднимет `marzban-node`.
3. Поставит зафиксированную версию xray-core (`node/XRAY_VERSION`) —
   актуальную для реальных клиентов, не то старьё, что вморожено в образ
   `gozargah/marzban-node:latest`.
4. Настроит UFW (443/8443 всем, 62050/62051 — только с IP панели, SSH).
5. Зарегистрирует ноду в панели (`add_as_new_host=true` — адрес сам попадёт
   в Hosts инбаундов).
6. **Прогонит честную проверку туннеля** — создаст одноразового юзера,
   пройдёт настоящий REALITY-хендшейк, удалит юзера. В конце явно скажет
   "ТУННЕЛЬ ПРОВЕРЕН" или "ТУННЕЛЬ НЕ ПРОШЁЛ" — не полагайся на "connected"
   в панели самой по себе, это разные вещи (см. `TROUBLESHOOTING.md`).

Если в самой панели ещё ни разу не был описан REALITY-инбаунд (первая нода в
жизни этой панели) — шаг 6 скажет "пропускаю проверку" и подскажет, что
делать: один раз описать инбаунд в Core Config панели (dest/serverNames/
privateKey/shortIds — см. `TROUBLESHOOTING.md`, там же живой пример), потом
прогнать `bash verify_node.sh` (с этого же VPS или удалённо, см. его шапку)
отдельно.

## 4. Если шаг 6 сказал "ТУННЕЛЬ НЕ ПРОШЁЛ"

Не игнорируй. Иди в `TROUBLESHOOTING.md` по порядку — #1 (версия xray-core)
почти всегда причина. Не публикуй ссылки на эту ноду клиентам, пока не
почини и не перепрогонишь `verify_node.sh` с результатом OK.

## 5. Переключить живые ссылки на новую ноду

Сама регистрация (`add_as_new_host=true`) уже добавила адрес новой ноды в
Hosts для тех инбаундов, которые были явно перечислены при регистрации
(bootstrap делает это автоматом). Если нужно ЗАМЕНИТЬ адрес у существующего
host-маппинга (типичный кейс миграции — старая нода умерла, новая её
замещает под тем же тегом инбаунда):

```bash
python3 - <<'PY'
import urllib.request, json, os
BASE = os.environ["PANEL_URL"]; TOKEN = os.environ["TOKEN"]
UA = "Mozilla/5.0"
def op(r): r.add_header("User-Agent", UA); return urllib.request.urlopen(r, timeout=30)
def api(m, p, body=None):
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(BASE + p, data=data, method=m)
    rq.add_header("Authorization", "Bearer " + TOKEN)
    if data is not None: rq.add_header("Content-Type", "application/json")
    r = op(rq); raw = r.read(); return json.loads(raw) if raw else {}

h = api("GET", "/api/hosts")
h["VLESS REALITY"][0]["address"] = "<НОВЫЙ_IP>"   # тег инбаунда — свой
api("PUT", "/api/hosts", h)
print("host обновлён")
PY
```

Все уже выданные клиентам ссылки автоматически поедут на новый IP —
подписка (`/sub/...`) генерируется на лету из текущего host-маппинга,
пересылать клиентам ничего не нужно.

## 6. Финальная сверка

```bash
# нода connected?
curl -s "$PANEL_URL/api/nodes" -H "User-Agent: Mozilla/5.0" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# и главное — реальный туннель, ещё раз, с самого свежего состояния панели:
PANEL_URL=... PANEL_USER=... PANEL_PASS=... NODE_IP=<новый_IP> NODE_SSH_PASS=... \
  bash node/verify_node.sh
```

Готово. Не забудь: `node/upgrade_xray.sh` — не разовая вещь, гоняй его снова
после любого `docker compose up --force-recreate`/пересоздания контейнера
ноды (live-патч бинарника живёт в writable layer, не в образе).
