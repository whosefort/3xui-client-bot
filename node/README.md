# Нода Marzban — авто-настройка

`bootstrap.sh` превращает свежий VPS в ноду Marzban и цепляет её к главной панели.

## Запуск (под root на чистом Ubuntu/Debian)
```bash
PANEL_URL=https://mon.your-domain.tld PANEL_USER=admin PANEL_PASS='...' \
  bash bootstrap.sh
```
Креды можно не задавать в env — скрипт спросит (пароль без эха).

Опциональные переменные:
- `NODE_NAME` — имя ноды в панели (по умолчанию hostname).
- `INBOUND_PORTS` — порты инбаундов открыть всем (по умолчанию `443` для REALITY).
- `SERVICE_PORT` / `XRAY_API_PORT` — порты ноды (62050 / 62051).

## Что делает
1. Ставит Docker.
2. Тянет client-cert панели (`GET /api/node/settings`) → `/var/lib/marzban-node/ssl_client_cert.pem`.
3. Поднимает `marzban-node` (docker, `network_mode: host`, protocol `rest`).
4. UFW: 443 всем, node-порты 62050/62051 — только с IP панели, SSH.
5. Регистрирует ноду (`POST /api/node`, `add_as_new_host=true`) — адрес ноды сам
   попадает в Hosts инбаундов, ссылки клиентов поедут на неё.

## После
- В панели нода станет `connected` за ~10–30 сек. Логи: `cd /opt/marzban-node && docker compose logs -f`.
- **REALITY-инбаунд описывается в САМОЙ панели** (Core Config / Hosts) — разово. Нода
  служит то, что панель раскатает.

## Безопасность
- Главная панель — «мозг», трафик не возит. Ноды — «скот», возят трафик, IP расходный.
- node-порты открыты только для IP панели. Прямой доступ к ним снаружи закрыт.
