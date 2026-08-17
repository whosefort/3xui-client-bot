# Нода Marzban — авто-настройка

`bootstrap.sh` превращает свежий VPS в ноду Marzban и цепляет её к главной панели.

## Запуск (под root на чистом Ubuntu/Debian)
```bash
PANEL_URL=https://mon.your-domain.tld bash bootstrap.sh
```
Скрипт спросит логин и пароль (пароль без эха); так секрет не попадёт в историю
команд. Для неинтерактивного запуска можно передать `PANEL_USER` и `PANEL_PASS`
через механизм секретов вашего CI/VPS-провайдера.

Опциональные переменные:
- `NODE_NAME` — имя ноды в панели (по умолчанию hostname).
- `INBOUND_PORTS` — порты инбаундов открыть всем (по умолчанию `443` для REALITY).
- `SERVICE_PORT` / `XRAY_API_PORT` — порты ноды (62050 / 62051).
- `PANEL_IP` — реальный IPv4/IPv6 origin-IP панели. Обычно определяется через DNS.
  Если панель за Cloudflare/CDN, DNS вернёт IP CDN, а соединения панель→нода идут с
  origin-IP. В этом случае переменная обязательна: например,
  `PANEL_IP=203.0.113.10 bash bootstrap.sh`. Скрипт не открывает management-порты
  всем и не пытается угадать origin по первому TCP-подключению.

## Что делает
1. Ставит Docker.
2. Тянет client-cert панели (`GET /api/node/settings`) → `/var/lib/marzban-node/ssl_client_cert.pem`.
3. Поднимает `marzban-node` (docker, `network_mode: host`, protocol `rest`).
4. Обновляет xray-core внутри контейнера до актуального тега XTLS (образ
   marzban-node часто отстаёт от того, что используют клиентские приложения —
   см. `TROUBLESHOOTING.md`).
5. UFW: 443 всем, node-порты 62050/62051 — только с IP панели, SSH.
6. Регистрирует ноду (`POST /api/node`, `add_as_new_host=true`) — адрес ноды сам
   попадает в Hosts инбаундов, ссылки клиентов поедут на неё.

## После
- В панели нода станет `connected` за ~10–30 сек. Логи: `cd /opt/marzban-node && docker compose logs -f`.
- **REALITY-инбаунд описывается в САМОЙ панели** (Core Config / Hosts) — разово. Нода
  служит то, что панель раскатает.
- Клиент не подключается / "не пингуется", хотя нода `connected`? →
  [`TROUBLESHOOTING.md`](./TROUBLESHOOTING.md) — сначала проверить версию
  xray-core (`upgrade_xray.sh`), это самая частая причина.

## Обслуживание
- `upgrade_xray.sh` — подтянуть свежий xray-core на уже работающей ноде, без
  полного bootstrap. Переживает `restart`/reboot, не переживает
  `docker compose up --force-recreate` — гонять заново после пересоздания
  контейнера.

## Безопасность
- Главная панель — «мозг», трафик не возит. Ноды — «скот», возят трафик, IP расходный.
- node-порты открыты только для IP панели. Прямой доступ к ним снаружи закрыт.
- Панель за CDN? Запусти скрипт с `PANEL_IP=<origin-IP>`. Иначе он завершится до
  настройки UFW; node-порты ни на каком этапе не открываются всем.
