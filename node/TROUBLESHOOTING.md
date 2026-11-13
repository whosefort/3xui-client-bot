# REALITY не пингуется — раннбук

Собрано по мотивам реального инцидента (2026-08-17): нода была развёрнута,
`connected` в панели, xray слушал 443, ручной тест туннеля с самой ноды проходил
100% — а живой клиент (Happ) не подключался вообще, ни разу, на двух разных
нодах в двух разных регионах. Ниже — что оказалось причиной и что было
отброшено по пути, по убыванию вероятности. Начинай с #1.

## Диагноз по логу клиента

Первым делом попроси лог клиента в момент попытки подключения. Он почти всегда
сразу говорит, где искать:

| В логе клиента | Значит |
|---|---|
| `REALITY: received real certificate (potential MITM or redirection)` | Сервер не распознал auth клиента и откатился на honest-relay к `dest`. См. #1. |
| Просто таймаут / нет ответа вообще | Порт не слушает, UFW/провайдер режет, или сеть клиента не пускает наружу. См. #3, #4. |
| `connection reset by peer` сразу после ClientHello | DPI/провайдер рвёт по паттерну, либо порт закрыт снаружи (но открыт локально — типичная ошибка UFW/облачного firewall). |

## #1. Рассинхрон версий xray-core клиент vs сервер (самая частая причина)

**Симптом:** `REALITY: received real certificate (potential MITM or redirection)`
у клиента, при этом ручной тест туннеля с самой ноды (см. ниже) проходит.

REALITY — протокол, который меняется (например добавление post-quantum
`mldsa65Seed`/`mldsa65Verify` полей). Клиентские приложения (Happ, v2rayNG и
т.п.) часто следят за **pre-release** тегами XTLS/Xray-core, а
`gozargah/marzban-node:latest` тащит xray-core, вмороженный в образ на момент
сборки — может отставать на много месяцев. Клиент шлёт хендшейк по новой
схеме, сервер её не понимает, молча откатывается на прозрачный проксинг к
`dest` (это штатное поведение REALITY при провале auth, не атака и не блок).

**Проверка:**
```bash
# версия у клиента — в его же логе, строка "Xray core X.Y.Z started"
# версия на ноде:
docker exec marzban-node-marzban-node-1 xray version
```
Если у клиента новее — вот причина.

**Фикс:**
```bash
# на самой ноде — подтянет версию из node/XRAY_VERSION (уже проверенную):
bash node/upgrade_xray.sh
# или руками конкретную версию, если проверяешь новую:
XRAY_VERSION=v26.7.28 bash node/upgrade_xray.sh
```
`bootstrap.sh` теперь делает это автоматически при первом разворачивании ноды
(шаг 3.5, тоже из `node/XRAY_VERSION`) — но клиентские приложения
обновляются быстрее, чем ты пересоздаёшь ноды, так что при новых жалобах на
"не пингуется" сначала сюда. `node/check_xray_drift.py` в кроне ловит это
само, без ручной проверки — см. `README.md`.

После установки новой версией bootstrap или первого обновления новым
`upgrade_xray.sh` бинарник хранится на хосте в
`/var/lib/marzban-node/xray-bin/xray` и переживает пересоздание контейнера.
Старые ноды переносятся в постоянный том при первом запуске обновлятора.

Проверил новую версию с живым клиентом и она работает? Обнови
`node/XRAY_VERSION` — это единственное место, откуда `bootstrap.sh`,
`upgrade_xray.sh` и `check_xray_drift.py` берут "текущую правильную версию".

## #2. `listen: ""` (пустая строка) вместо `"0.0.0.0"`

**Симптом:** нода `connected` в панели, но xray на ней в crash-loop
(`panic: runtime error: index out of range [0] with length 0`).

На Xray-core 25.x пустой `listen` в REALITY-инбаунде роняет процесс при старте.
Некоторые референсные конфиги (в т.ч. из 3x-ui) используют `listen: ""` и у них
это работает — но версия xray-core там другая. На marzban-node — **всегда**
`"listen": "0.0.0.0"`, никогда `""`.

**Проверка:**
```bash
docker exec marzban-node-marzban-node-1 xray run -test -config <(echo '<конфиг>')
# или просто: слушает ли порт вообще
ss -tlnp | grep :443
```

## #3. Мастер-панель крашит свой локальный core (шум, но не блокер трафика)

**Симптом:** в логе `marzban` (панели) бесконечно повторяется
`Restarting Xray core...` каждые ~10 сек, без явной ошибки в WARNING-строках.

Marzban всегда пытается поднять **тот же** конфиг локально на мастере, что
пушит на ноды — включая REALITY-инбаунд на 443. Если мастер уже занял 443 под
себя (панель/CF), локальный xray мастера не может забиндиться и падает в
цикле. Это **не влияет на трафик через ноду** (нода — отдельная машина,
конфликта портов нет), но:
- захламляет логи;
- в старых версиях Marzban `core_health_check()` не оборачивает рестарт
  локального core в try/except — если он падает, весь цикл проверки **ноды**
  может не доехать до раздела `nodes' core` в том же вызове. Патч —
  `panel/Dockerfile` в этом репозитории, блок с `core_health_check`.

**Проверка причины краша:** зайти в контейнер панели и руками прогнать
xray с реальным конфигом:
```bash
docker exec -w /code marzban-marzban-1 python3 -c '
import sys; sys.path.insert(0,"/code")
from app import xray
open("/tmp/cfg.json","w").write(xray.config.include_db_users().to_json())'
docker exec marzban-marzban-1 sh -c "timeout 4 xray run -config /tmp/cfg.json 2>&1"
```
Смотри на `Failed to start: ...` — там реальная причина (было два случая:
хардкод-путь к логу `/var/lib/marzban-node/...`, которого нет на мастере, и
`bind: address already in use` на 443).

## Что НЕ было причиной (проверено и отброшено)

Не трать время на это заново, если #1–#3 не помогли и клиент всё ещё молчит:

- **Cloudflare-проксирование панели.** REALITY идёт напрямую на IP ноды,
  минуя CF полностью — домен панели тут ни при чём.
- **Гео/ASN хостинга (US vs EU).** Проверялось на реальном примере: одна и та
  же нода на одном и том же хостере (RouterHosting), Даллас vs Франкфурт —
  обе одинаково не пинговались до фикса #1, обе одинаково заработали после.
  Не гео, не DPI по подсети.
- **Fragment/Noise в клиенте.** Выключение не помогло — эти настройки для
  голого VLESS+TLS, REALITY в них не нуждается, но и не ломается без причины
  #1 в наличии.
- **shortId/dest/nested-settings отличия от референсного 3x-ui конфига.**
  Смысловой эквивалент (`dest` — современный алиас `target`) на актуальном
  xray-core работает так же.

## Как проверить туннель самому, не дожидаясь живого клиента

Проще всего: `bash node/verify_node.sh` — делает ровно это автоматически
(создаёт одноразового юзера через API панели, гоняет туннель, удаляет юзера,
выводит вердикт). `bootstrap.sh` уже гоняет его на новых нодах сам.

Ручной способ (если нужно руками/для отладки, без скрипта) — аутентифицированный
тест прямо с ноды, на её публичный IP (не loopback — иначе не поймаешь ни
firewall, ни version-skew с реальным путём):

```bash
# /var/lib/marzban-node — единственный путь, замаунтенный И на хосте, И в
# контейнере (docker-compose.yml). /tmp хоста контейнер не видит.
cat > /var/lib/marzban-node/client.json <<'EOF'
{"log":{"loglevel":"warning"},
 "inbounds":[{"port":10999,"listen":"127.0.0.1","protocol":"socks","settings":{"udp":true}}],
 "outbounds":[{"protocol":"vless","settings":{"vnext":[{"address":"<ПУБЛИЧНЫЙ_IP_НОДЫ>","port":443,
   "users":[{"id":"<UUID_КЛИЕНТА>","encryption":"none","flow":"xtls-rprx-vision"}]}]},
   "streamSettings":{"network":"tcp","security":"reality","realitySettings":{
     "serverName":"<SNI>","fingerprint":"chrome",
     "publicKey":"<PBK>","shortId":"<SID>","spiderX":"/"}}}]}
EOF
cd /opt/marzban-node
docker compose exec -d marzban-node xray run -config /var/lib/marzban-node/client.json
sleep 3
# curl — С ХОСТА, не docker exec: в образе marzban-node нет curl/wget вообще.
# network_mode: host расшаривает сеть, сокет виден и достижим напрямую.
curl --socks5-hostname 127.0.0.1:10999 -s -o /dev/null -w "http=%{http_code}\n" --max-time 10 https://www.gstatic.com/generate_204
# прибраться: не pkill (его тоже нет в образе) — найти PID по порту и убить точечно
PID=$(ss -tlnp 2>/dev/null | grep '127.0.0.1:10999' | grep -oE 'pid=[0-9]+' | cut -d= -f2)
[ -n "$PID" ] && kill "$PID"
rm -f /var/lib/marzban-node/client.json
```
`http=204` — сервер и авторизация в порядке, проблема дальше по пути (клиент,
его сеть, DPI конкретно у клиента). Не 204 — проблема на самой ноде, копай
#1/#2.

**Осторожно:** в этом же контейнере всегда крутится СВОЙ основной xray
(реально держит 443 для живых клиентов, cmdline `xray run -config stdin:`).
`pkill xray`/`pkill -f xray` заденет и его — совпадает по имени процесса.
В образе marzban-node `pkill` в любом случае нет (ранняя версия
`verify_node.sh` полагалась на него, тихо проваливалась и копила зомби-
процессы — не роняла прод только по счастливой случайности отсутствия
самой команды). Только точечный `kill` по PID конкретного порта, как выше.

**Важно:** тестируй с чужой машины (check-host.net, другой VPS), не со своего
домашнего провайдера — многие бытовые провайдеры SYN-ACK'ают весь диапазон
портов на любой IP, что даёт ложное "порт открыт" при внешнем сканировании.

---

## Просадка скорости у всех клиентов: `nf_conntrack: table full`

**Симптом.** Клиенты подключаются, но скорость падает до единиц Мбит/с и ниже
(реальный случай 2026-11-04: было <1 Мбит/с, после фикса — ~20 Мбит/с). Часть
новых соединений не открывается вообще, страницы и видео зависают, старые
загрузки «умирают» на середине, спидтест может не стартовать. Провайдер и
канал при этом ни при чём: сама нода отдаёт в интернет 200+ Мбит/с.

**Причина.** Дефолтная таблица conntrack на ноде — **8192 записи**, а
`nf_conntrack_tcp_timeout_established` — **5 дней** (432000 с). Клиенты на
мобильных и спящих ноутбуках рвут соединения без FIN/RST; записи не
освобождаются, таблица стоит полной. Когда она полна, ядро дропает пакеты
**новых** соединений:

```
nf_conntrack: nf_conntrack: table full, dropping packet
```

Дополнительный маркер: UFW дропает `ctstate INVALID` — это пакеты потоков,
у которых conntrack уже потерял запись. Для клиента это выглядит как
оборванная на середине загрузка.

### Диагностика (read-only)

```bash
sysctl net.netfilter.nf_conntrack_max net.netfilter.nf_conntrack_count \
       net.netfilter.nf_conntrack_tcp_timeout_established
# count == max и timeout_established == 432000 → это оно

dmesg -T | grep -c "table full"
dmesg -T | grep "table full" | tail -3

ss -s
# при полной таблице: сокетов ~700, а conntrack_count 8192 — записи «мёртвые»

ss -tin dst <IP_КЛИЕНТА> | grep -E "cwnd|rto|retrans"
# застрявший поток: cwnd:1, rto:120000 backoff:15, retrans:1/202, notsent:2.5M

# скорость самой ноды (не должна быть низкой):
curl -s -o /dev/null -w "%{speed_download} B/s\n" --max-time 40 \
  https://ash-speed.hetzner.com/100MB.bin
```

Быстрая проверка одной командой: `bash node/check_conntrack.sh` (запускать на
ноде; read-only).

### Фикс

```bash
modprobe nf_conntrack 2>/dev/null || true
cat > /etc/sysctl.d/99-vpn-conntrack.conf <<'EOF'
net.netfilter.nf_conntrack_max = 262144
net.netfilter.nf_conntrack_tcp_timeout_established = 7440
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 60
net.netfilter.nf_conntrack_tcp_timeout_syn_sent = 30
net.netfilter.nf_conntrack_tcp_timeout_syn_recv = 30
EOF
sysctl --system
```

- `nf_conntrack_max` применяется на лету; таблица разгружается сразу.
- `tcp_timeout_established = 7440` (2 часа) отпускает мёртвые потоки, не
  трогая активные: запись обновляется на каждом пакете.
- `nf_conntrack_buckets` не трогаем: меняется только при перезагрузке модуля,
  а 8192 бакетов на 262144 записей — приемлемо (средняя цепочка ~32).

### Проверка после фикса

```bash
sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max
# count сильно ниже max (в реальном случае 392–757 из 262144)

dmesg -T | grep "table full" | tail -1
# новых сообщений после времени правки быть не должно

for i in $(seq 1 60); do
  curl -s -o /dev/null --max-time 5 -w "%{http_code}\n" https://1.1.1.1/ &
done; wait
# ожидаем 60/60 ok; до фикса часть новых соединений дропалась
```

### Откат

При ручном применении на ноде создаётся `/root/conntrack-rollback.sh`:

```bash
/root/conntrack-rollback.sh   # вернёт 8192 / 432000 / 120 / 120 / 60 и удалит sysctl-файл
```

Откат применяется на лету, без перезагрузки. Но он возвращает и баг: таблица
снова начнёт заполняться мёртвыми потоками.

### Где это уже автоматизировано

- `node/bootstrap.sh` — блок «4.6. conntrack» (ручная установка ноды).
- `node/bootstrap_token.sh` — блок «5.6. conntrack» (авторазвёртывание из
  админки бота). **Важно:** этот файл новые ноды качают с GitHub main, поэтому
  фикс доедет до них только после коммита и пуша.
- `xui/bootstrap.sh` — блок «3.5. conntrack» (второй сервер с 3x-ui).

### Реальный кейс (2026-11-04)

Нода `eu-frankfurt-01` (172.86.66.169, RouterHosting): `8192/8192`, 1409
сообщений `table full` за день, 5,1% TCP-ретрансмиссий за 79 дней, у клиента
с Ростелекома скорость <1 Мбит/с. После фикса: `392–757/262144`, новых
`table full` нет, скорость клиента ~20 Мбит/с, память и CPU ноды без
изменений.
