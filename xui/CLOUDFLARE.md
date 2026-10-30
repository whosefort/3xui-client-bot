# Поднятие VPN-инбаундов через Cloudflare (3x-ui / Xray)

Практический runbook на основе рабочей конфигурации сервера
`203.0.113.20` (3x-ui 3.6, Xray 26.7.x), домен `твой-домен.tld` на Cloudflare.

---

## 1. Что вообще можно фронтить через Cloudflare, а что нет

Cloudflare (бесплатный план) — это **обратный HTTP(S)-прокси поверх TCP**. Он
терминирует TLS на своём edge и передаёт запрос на origin. Отсюда правило:

| Транспорт | Через CF? | Комментарий |
|---|---|---|
| **VLESS/Trojan + WebSocket (ws) + TLS** | ✅ да | классика, максимальная совместимость (Shadowrocket, Happ, v2ray…) |
| **VLESS + gRPC + TLS** | ✅ да | тоже поверх HTTP/2, работает |
| **VLESS + XHTTP (SplitHTTP) + TLS** | ✅ да | современная замена ws, лучше держит нестабильную сеть |
| **REALITY** | ❌ нет | это сырой TCP + подмена TLS; CF-прокси его убивает. Только напрямую на IP |
| **Hysteria / Hysteria2 / TUIC** | ❌ нет | UDP/QUIC. CF free UDP не проксирует (нужен платный Spectrum). Только напрямую |
| **Shadowsocks, WireGuard** | ❌ нет | не HTTP, CF не проксирует |

Вывод: через Cloudflare идут только **TCP-транспорты поверх HTTP(S)** — ws / grpc / xhttp.
Всё UDP и REALITY — мимо CF, напрямую на IP сервера.

---

## 2. Зачем вообще CF-фронт

- Клиент видит TLS-соединение к **IP Cloudflare** с SNI твоего домена → для DPI это
  обычный трафик к сайту на Cloudflare. Сигнатуры прокси не видно.
- **Не нужна клиентская фрагментация/noise** (в отличие от REALITY напрямую).
- Origin-IP спрятан за CF (если закрыть порт origin только для CF — см. §6).

⚠️ **Оговорка для РФ:** РКН активно душит диапазоны Cloudflare (2024–2026). Соединение
до CF устанавливается, но данные могут не идти. Если из РФ «коннект есть, загрузки нет» —
см. §9 (чистый CF-IP) или думать про другой CDN (Gcore).

---

## 3. Предусловия

1. Домен, чьи NS указывают на Cloudflare (зона активна в CF).
2. Доступ к дашборду Cloudflare этого домена.
3. 3x-ui / Xray на сервере, root-SSH.

---

## 4. Cloudflare: DNS-запись

Dashboard → **DNS → Records → Add record**:

- Type: `A`
- Name: поддомен, напр. `ws` (будет `ws.твой-домен.tld`)
- IPv4: IP сервера (`203.0.113.20`)
- Proxy status: **Proxied** (оранжевое облако ☁️) — обязательно

> Apex-домен и другие записи, на которые смотрит REALITY, держи **DNS only** (серое
> облако). REALITY через оранжевое облако не работает.

---

## 5. Cloudflare: TLS-режим и сертификат origin

### 5.1 SSL/TLS mode
Dashboard → **SSL/TLS → Overview → Configure → `Full (strict)`**.
(Применяется только к проксируемым/оранжевым записям.)

- `Flexible` — CF идёт на origin по **HTTP** → не будет работать, если origin на TLS. Не использовать.
- `Full` / `Full (strict)` — CF идёт на origin по HTTPS. Нужен серт на origin.

### 5.2 Origin-сертификат
Dashboard → **SSL/TLS → Origin Server → Create Certificate**
→ оставить RSA, hostnames `*.твой-домен` и `твой-домен` → Create.
Скопировать **оба** блока (Certificate и Private Key).

На сервере:
```bash
mkdir -p /root/cert/cf-origin
# вставить сертификат:
nano /root/cert/cf-origin/fullchain.pem
# вставить приватный ключ:
nano /root/cert/cf-origin/privkey.pem
chmod 600 /root/cert/cf-origin/privkey.pem
chmod 644 /root/cert/cf-origin/fullchain.pem
```
Проверка, что серт и ключ — пара (md5 должны совпасть):
```bash
openssl x509 -noout -modulus -in /root/cert/cf-origin/fullchain.pem | openssl md5
openssl rsa  -noout -modulus -in /root/cert/cf-origin/privkey.pem   | openssl md5
```

---

## 6. Firewall: пустить CF на origin-порт (и только CF)

Если на сервере активен UFW с `policy DROP`, порт origin-инбаунда будет закрыт для CF —
и CF не достучится (симптом: `curl https://домен:ПОРТ` → `http=000`).

Открыть origin-порт (пример 8443) **только для диапазонов Cloudflare** — так порт не
виден прямым сканерам:
```bash
CF4="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22"
CF6="2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"
for ip in $CF4 $CF6; do ufw allow proto tcp from $ip to any port 8443 comment 'CF-origin'; done
```
> Актуальные диапазоны: https://www.cloudflare.com/ips/

---

## 7. Порты Cloudflare (важно!)

CF проксирует только определённые порты. На origin слушаем ту же цифру, что и edge.

- **HTTPS edge-порты:** `443, 2053, 2083, 2087, 2096, 8443`
- **HTTP edge-порты:** `80, 8080, 8880, 2052, 2082, 2086, 2095`

⚠️ **Конфликт с REALITY на 443:** если REALITY уже на 443, WS-origin вешаем на другой
CF-порт (мы взяли **8443**). Клиент тогда коннектится на `домен:8443`.
Хочешь клиентам порт 443 — нужен **Origin Rule** в CF (edge 443 → origin 8443),
т.к. напрямую edge 443 → origin 443 займёт REALITY.

---

## 8. Создание инбаунда (VLESS + WS + TLS)

В панели 3x-ui: **Inbounds → Add**:
- Protocol: `VLESS`
- Listen: пусто (0.0.0.0), Port: `8443`
- Clients: добавить (flow оставить **пустым** — vision только для REALITY!)
- Transport: `ws`, path напр. `/assets/stream/`, Host: `ws.твой-домен`
- Security: `TLS`
- SNI/serverName: `ws.твой-домен`
- Certificate: путь `/root/cert/cf-origin/fullchain.pem` + `privkey.pem`
- ALPN: `http/1.1`
- uTLS/fingerprint (если есть): `chrome`

### 8.1 externalProxy — чтобы ссылка указывала на CF-edge
Чтобы сгенерированная ссылка/подписка вела на `домен:edge-порт` (а не на origin-IP),
в настройках потока инбаунда задать **External Proxy**:
- Dest: `ws.твой-домен`
- Port: `8443` (edge-порт)
- forceTls: `tls`

Без этого подписка подставит origin-адрес, и клиент пойдёт мимо CF.

### 8.2 Анатомия рабочей клиентской ссылки
```
vless://UUID@ws.твой-домен:8443?encryption=none&security=tls&sni=ws.твой-домен&fp=chrome&type=ws&host=ws.твой-домен&path=%2Fassets%2Fstream%2F&alpn=http%2F1.1#remark
```
- `security=tls` (НЕ reality)
- `type=ws`, `path` и `host` совпадают с инбаундом
- `sni` = домен, `flow` отсутствует

---

## 9. «Чистый» CF-IP (если из РФ душит)

Cloudflare — anycast: клиент может коннектиться к **конкретному** CF-IP, сохраняя
SNI/Host домена. Часть адресов РКН душит меньше.

- Клиент из РФ прогоняет сканер (CloudflareST / bestip) → находит лучший CF-IP.
- В ссылке меняем только адрес подключения на этот IP (sni/host остаются доменом):
```
vless://UUID@<чистый-CF-IP>:8443?...&sni=ws.твой-домен&host=ws.твой-домен&type=ws&path=...
```
- Можно зашить в подписку через externalProxy Dest = этот IP.

---

## 10. Диагностика (чек-лист)

Прогонять с сервера (или любой машины с сетью):

```bash
# 1. Домен резолвится в CF? (должны быть 104.x / 172.6x / 188.114.x)
getent hosts ws.твой-домен

# 2. CF доходит до origin? (после SSL Full + firewall) — код НЕ должен быть 000
curl -s -4 -m 15 -o /dev/null -w "%{http_code}\n" https://ws.твой-домен:8443/

# 3. origin отдаёт origin-серт локально?
echo | openssl s_client -connect 127.0.0.1:8443 -servername ws.твой-домен 2>/dev/null | openssl x509 -noout -subject
```

| Симптом | Причина | Лечение |
|---|---|---|
| `curl ...:порт` → `http=000` | firewall закрыл порт для CF, ИЛИ SSL mode = Flexible | §6 (открыть порт для CF), §5.1 (Full strict) |
| CF-ошибка **521/522** | origin недоступен / порт закрыт | firewall / инбаунд не слушает |
| CF-ошибка **525/526** | TLS CF↔origin не сложился | серт origin (§5.2) или режим Full strict |
| edge :8443 отдаёт серт, но GET виснет | CF не проксирует этот порт, либо origin не на TLS | §7 (правильный порт), security=tls |
| коннект есть, данные не идут **только из РФ** | РКН душит CF | §9 (чистый IP) или другой CDN |

---

## 11. Что НЕ фронтить через CF (важно для безопасности IP)

- **REALITY** — только напрямую на IP. Он и так probe-resistant (fallback на реальный сайт).
- **Hysteria2 / TUIC / любой UDP** — только напрямую. Учти: прямой UDP-протокол
  **светит origin-IP** и при детекте РКН может привести к бану IP — а на этом IP живёт
  и рабочий REALITY. Рискованные прямые протоколы лучше держать на **отдельном IP**,
  чтобы не «сжечь» основной сервер.

---

## Пример: как это было устроено на рабочем сервере (для справки)

- `:443` — VLESS **REALITY** (dl.google.com), напрямую. Стабильно работает из РФ.
- `:8443` — VLESS **WS+TLS** за Cloudflare (`ws.твой-домен.tld`), origin-серт CF,
  firewall открыт только для CF. Проверен снаружи (YouTube, крупные файлы — ок).
  Из РФ упирается в throttling Cloudflare (см. §9).
- Бэкапы БД: `/root/x-ui.db.bak.*`
