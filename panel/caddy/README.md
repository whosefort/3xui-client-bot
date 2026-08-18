# Caddy — TLS-фронт мастера (mon. + sub.)

Зачем — см. `../README.md`, раздел «Безопасность: подписка мимо Cloudflare».
Тут — конкретные команды установки/продления/восстановления.

## Установка с нуля — автоматом

Один скрипт делает всё: ставит Caddy/certbot/acl, правит `/opt/marzban/.env`,
получает Let's Encrypt серт под sub.-домен, настраивает ACL и автопродление,
генерирует `Caddyfile` из шаблона и запускает. Идемпотентный — можно гонять
повторно (например, после смены домена).

```bash
scp -r panel/caddy root@<мастер>:/root/caddy-setup
ssh root@<мастер> '
  MON_DOMAIN=mon.ваш-домен.tld \
  SUB_DOMAIN=sub.ваш-домен.tld \
  /root/caddy-setup/setup.sh
'
```

Переменные (см. шапку `setup.sh`): `MON_DOMAIN`, `SUB_DOMAIN` — обязательны;
`MON_CERT_DIR` (по умолчанию `/var/lib/marzban/certs`, серт для дашборд-домена
должен уже лежать там как `fullchain.pem`+`key.pem` — обычно Cloudflare Origin
CA), `MARZBAN_ENV` (по умолчанию `/opt/marzban/.env`).

Перед запуском: в Cloudflare `SUB_DOMAIN` — DNS only (серое облако),
`MON_DOMAIN` — Proxied (оранжевое), обе A-записи смотрят на IP мастера.

Дальше руками — то, что скрипт не трогает:
```bash
cd /opt/marzban && docker compose up -d   # НЕ restart — не подхватит .env
certbot renew --dry-run                   # проверить, что автопродление реально работает
```

## ⚠️ Готча: `certbot renew` виснет / отдаёт 404 на challenge-путь

Даже если сертификаты подключены через explicit `tls <файл>`, Caddy всё равно
по умолчанию вешает СВОЙ ACME HTTP-01 solver на `/.well-known/acme-challenge/*`
для любого домена, упомянутого в site-блоках — и он перехватывает запрос
**до** пользовательского `handle{}`. В логах (`journalctl -u caddy`) это видно
как `"looking up info for HTTP challenge" ... "no information found to solve
challenge"` — Caddy сам себе мешает, это не баг в конфиге handle-блока.

Из-за этого `certbot renew --webroot` виснет на встроенной рандомной паузе
(certbot специально ждёт 300–450 сек перед non-interactive попыткой, чтобы не
долбить LE одновременно с другими серверами — это НЕ зависание, дай ему
доработать), а затем реальная попытка получает 404 на challenge-файл.

**Фикс** — глобально отключить автоматический HTTPS у Caddy (сертификаты и
так подключены вручную, он не нужен) и взять порт 80 под ручной контроль:
```
{
    auto_https off
}
```
плюс явные `http://` site-блоки с ручным редиректом на https для КАЖДОГО
домена (см. `Caddyfile.tmpl` в этой папке — без этого пропадёт автоматический
http→https редирект, который раньше давал `auto_https`). `setup.sh` уже
собирает конфиг именно так.

Отдельная ловушка при ручном тестировании этого пути: certbot кладёт challenge-
файл не в корень `root`, а во вложенный `<root>/.well-known/acme-challenge/<token>`
(webroot-плагин сам создаёт эту структуру) — если проверяешь curl'ом руками,
клади тестовый файл туда же, иначе получишь 404 по своей же ошибке, а не из-за
конфига.

## Восстановление после падения/ребута

Caddy — systemd-сервис (`systemctl status caddy`), поднимается сам. Единственная
реальная ловушка — гонка порта 443 с локальным xray-core мастера (см.
`../README.md`): если после ребута Caddy не стартовал с `address already in
use` — `ss -tlnp | grep :443`, убей xray, `systemctl start caddy`.

## Диагностика

```bash
systemctl status caddy --no-pager -l
journalctl -u caddy -n 50 --no-pager
caddy validate --config /etc/caddy/Caddyfile
echo | openssl s_client -connect sub.ваш-домен.tld:443 -servername sub.ваш-домен.tld \
  2>/dev/null | openssl x509 -noout -issuer -dates   # должен быть Let's Encrypt, не Cloudflare
```
