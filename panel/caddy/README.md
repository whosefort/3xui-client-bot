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

## Happ per-subscription routing

Happ переехал на роутинг, привязанный к конкретной подписке — без готового
профиля от провайдера клиенту приходится настраивать bypass-правила в
приложении руками. Поэтому sub.-домен отдаёт заголовок `routing` с готовым
`happ://routing/onadd/...`.

Значение не впаяно в Caddyfile: Caddy читает его из
`/var/lib/happ-routing/happ_routing.deeplink` на каждый запрос (плейсхолдер
`{file.…}`, нужен Caddy >= 2.8), а сам файл пересобирает бот
(`bot/happ_routing.py`) при старте и после каждой правки:

- **база** — `panel/happ_routing.deeplink` из репо (основа из
  [roscomvpn-routing](https://github.com/hydraponique/roscomvpn-routing):
  RU/BY и торренты напрямую, YouTube/Telegram/GitHub через прокси,
  реклама/телеметрия блокируются);
- **свои direct/proxy-домены** — добавляются в админке бота: 🛡 Соединение →
  ↪️ Direct-домены (мимо VPN) и 🔀 Proxy-домены (явно через VPN). Хранятся в
  БД бота отдельно от базы, обновление базы их не затирает.

Ни reload Caddy, ни SSH для правки списка не нужны. Обновить базу: заменить
`panel/happ_routing.deeplink` и передеплоить бота (`./deploy.sh`) — он
выложит свежий файл при старте. Каталог `/var/lib/happ-routing` (владелец
uid 10001, читается всеми) создают `setup.sh` и `deploy.sh`; `setup.sh`
кладёт туда базу как затравку, чтобы заголовок был непустым ещё до первого
старта бота.

## Гео-файлы Happ со своего домена (/geo/*)

По умолчанию Happ качает geoip.dat/geosite по jsDelivr-URL из профиля — а
jsDelivr для части RU-клиентов недоступен или медленный, и Happ рвёт
загрузку по таймауту (~3 мин) с ошибкой у профиля. Поэтому те же файлы
раздаёт сам sub-домен (блок `handle_path /geo/*` в `Caddyfile.tmpl`, корень
`/var/www/happ-geo`):

1. `panel/caddy/fetch_geo.sh` кладёт в `/var/www/happ-geo` ровно те версии,
   что запинены в `panel/happ_routing.deeplink` (сверяет sha256, замена
   атомарная, идемпотентный). Запуск на мастере: `bash panel/caddy/fetch_geo.sh`.
   На автообновление — cron: `23 4 * * 0 bash /root/3xui-client-bot/panel/caddy/fetch_geo.sh`.
2. В `.env` бота: `HAPP_GEO_BASE_URL=https://<sub-домен>/geo` — тогда бот при
   сборке профиля подменяет Geoipurl/Geositeurl на свои URL
   (`bot/happ_routing.py`). Пусто — клиенты качают напрямую с jsDelivr.
3. Перезапуск бота (`docker compose up -d --build`) — профиль переиздаётся с
   новым LastUpdated, Happ подтянет его при следующем обновлении подписки и
   скачает гео-файлы с sub-домена.

Смысл: подписка и гео-файлы идут с одного хоста — если клиент смог получить
подписку, то и рулсет скачается.

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
