# Caddy — TLS-фронт мастера (mon. + sub.)

Зачем — см. `../README.md`, раздел «Безопасность: подписка мимо Cloudflare».
Тут — конкретные команды установки/продления/восстановления.

## Установка с нуля

```bash
# 1. Caddy из официального репозитория
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy certbot acl

# 2. Marzban перестаёт биндить публичный 443 напрямую — в /opt/marzban/.env:
#    UVICORN_HOST = "127.0.0.1"
#    UVICORN_PORT = 8001
#    # UVICORN_SSL_CERTFILE / UVICORN_SSL_KEYFILE — закомментировать, TLS теперь у Caddy
cd /opt/marzban && docker compose up -d   # НЕ restart — не подхватит .env

# 3. Порт 443 у мастера сейчас, скорее всего, держит локальный xray-core
#    Marzban (см. ../README.md про гонку портов) — освободи его перед стартом Caddy:
ss -tlnp | grep :443
kill -9 <pid если это xray>

# 4. Первый серт для sub.-домена — ПОКА Caddy не запущен, порт 80 свободен:
mkdir -p /var/www/acme-challenge
certbot certonly --standalone --non-interactive --agree-tos \
  --register-unsafely-without-email -d sub.ваш-домен.tld

# 5. ACL — caddy работает не под root, дефолтный ACL на архив сертов:
setfacl -d -m u:caddy:r /etc/letsencrypt/archive/sub.ваш-домен.tld/
setfacl -m u:caddy:r /etc/letsencrypt/archive/sub.ваш-домен.tld/privkey1.pem
setfacl -m u:caddy:r /etc/letsencrypt/archive/sub.ваш-домен.tld/fullchain1.pem
setfacl -m u:caddy:x /etc/letsencrypt/archive /etc/letsencrypt/live

# 6. Конфиг Caddy (подставь свои домены) → запуск:
scp Caddyfile root@<мастер>:/etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl start caddy

# 7. Переключить продление certbot на webroot (standalone конфликтует с Caddy
#    за порт 80 при каждом renew):
sed -i 's/^authenticator = standalone/authenticator = webroot/' \
  /etc/letsencrypt/renewal/sub.ваш-домен.tld.conf
cat >> /etc/letsencrypt/renewal/sub.ваш-домен.tld.conf <<'EOF'
[[webroot_map]]
sub.ваш-домен.tld = /var/www/acme-challenge
EOF

# 8. Хук: после продления перевыдать ACL на новый номерной файл + перечитать Caddy
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-caddy.sh <<'EOF'
#!/bin/sh
setfacl -m u:caddy:r /etc/letsencrypt/archive/sub.ваш-домен.tld/privkey*.pem \
                     /etc/letsencrypt/archive/sub.ваш-домен.tld/fullchain*.pem 2>/dev/null
systemctl reload caddy
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-caddy.sh

# 9. Проверка, что продление реально сработает (без реального продления):
certbot renew --dry-run

# 10. Marzban: XRAY_SUBSCRIPTION_URL_PREFIX = "https://sub.ваш-домен.tld"
cd /opt/marzban && docker compose up -d
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
домена (см. `Caddyfile` в этой папке — без этого пропадёт автоматический
http→https редирект, который раньше давал `auto_https`).

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
