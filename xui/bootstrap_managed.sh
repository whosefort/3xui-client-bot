#!/usr/bin/env bash
# This script is streamed over authenticated SSH. Its stdout is a private protocol.
set -euo pipefail
umask 077
domain=$1
image=$2
mode=${3:-dns}
[[ "$mode" == ip || "$mode" == dns ]]
[[ "$domain" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ ]]
[[ "$image" =~ ^ghcr\.io/mhsanaei/3x-ui@sha256:[0-9a-f]{64}$ ]]
[[ $(id -u) == 0 ]]

state=/opt/bot-managed-3xui
container=bot-managed-3xui
exec 3>&1 4>&2
phase=preflight
trap 'printf "3x-ui setup failed: %s (line %s)\n" "$phase" "$LINENO" >&4' ERR
exec >/dev/null 2>&1
if [[ ! -f $state/target ]]; then
    [[ ! -e /etc/x-ui/x-ui.db && ! -e /usr/local/x-ui && ! -e /etc/caddy/Caddyfile ]]
    ! ss -H -ltn '( sport = :80 or sport = :443 or sport = :2053 or sport = :2096 or sport = :24443 )' | grep -q .
    if command -v docker >/dev/null; then
        [[ -z $(docker ps -aq) ]]
    fi
    install -d -m 0700 "$state/db"
    printf '%s\n' "$domain" > "$state/target"
fi
[[ $(cat "$state/target") == "$domain" ]]
exec 9>"$state/setup.lock"
flock -n 9
export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null || ! command -v caddy >/dev/null; then
    apt-get -o Acquire::Retries=2 update
    apt-get install -y docker.io caddy openssl
fi
systemctl enable --now docker
if [[ ! -s $state/path ]]; then
    openssl rand -hex 24 > "$state/admin-password"
    openssl rand -hex 16 > "$state/path"
fi
path=$(cat "$state/path")
tls=""
http_block=""
if [[ "$mode" == ip ]]; then
    phase=certbot
    apt-get install -y python3-venv
    if [[ ! -x "$state/certbot/bin/certbot" ]]; then
        python3 -m venv "$state/certbot"
        "$state/certbot/bin/pip" install 'certbot==5.4.0'
    fi
    install -d -m 0755 /var/lib/xui-acme /var/lib/xui-acme/.well-known \
        /var/lib/xui-acme/.well-known/acme-challenge
    http_block="http://$domain {
    @acme path /.well-known/acme-challenge/*
    handle @acme {
        root * /var/lib/xui-acme
        file_server
    }
    handle {
        redir https://$domain{uri} 308
    }
}"
    if [[ ! -s "/etc/letsencrypt/live/$domain/fullchain.pem" ]]; then
        printf '%s\n' "$http_block" > /etc/caddy/Caddyfile
        systemctl enable --now caddy
        systemctl reload caddy
        phase=certificate
        (umask 022
         "$state/certbot/bin/certbot" certonly --non-interactive --agree-tos \
            --register-unsafely-without-email --preferred-profile shortlived --webroot \
            --webroot-path /var/lib/xui-acme --ip-address "$domain")
    fi
    install -d -m 0750 -o root -g caddy /etc/caddy/xui-tls
    cat > "$state/deploy-cert" <<'HOOK'
#!/bin/sh
set -eu
install -m 0640 -o root -g caddy "$RENEWED_LINEAGE/fullchain.pem" /etc/caddy/xui-tls/fullchain.pem
install -m 0640 -o root -g caddy "$RENEWED_LINEAGE/privkey.pem" /etc/caddy/xui-tls/privkey.pem
caddy reload --config /etc/caddy/Caddyfile --force
HOOK
    chmod 0700 "$state/deploy-cert"
    install -m 0640 -o root -g caddy "/etc/letsencrypt/live/$domain/fullchain.pem" /etc/caddy/xui-tls/fullchain.pem
    install -m 0640 -o root -g caddy "/etc/letsencrypt/live/$domain/privkey.pem" /etc/caddy/xui-tls/privkey.pem
    tls="tls /etc/caddy/xui-tls/fullchain.pem /etc/caddy/xui-tls/privkey.pem"
    cat > /etc/systemd/system/xui-certbot.service <<EOF
[Unit]
Description=Renew the 3x-ui IP certificate
[Service]
Type=oneshot
UMask=0022
ExecStart=$state/certbot/bin/certbot renew --quiet --deploy-hook $state/deploy-cert
EOF
    cat > /etc/systemd/system/xui-certbot.timer <<'EOF'
[Unit]
Description=Check 3x-ui IP certificate every six hours
[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
Persistent=true
RandomizedDelaySec=10m
[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now xui-certbot.timer
fi
if ! docker container inspect "$container" >/dev/null 2>&1; then
    phase=image
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        timeout 240 docker pull "$image"
    fi
    phase=database
    docker run --rm --entrypoint /app/x-ui -v "$state/db:/etc/x-ui" "$image" \
        setting -username admin -password "$(cat "$state/admin-password")" -port 2053 -webBasePath "/$path/"
    docker run -d --name "$container" --restart unless-stopped \
        -e XUI_ENABLE_FAIL2BAN=false -v "$state/db:/etc/x-ui" \
        -p 127.0.0.1:2053:2053 -p 127.0.0.1:2096:2096 -p 24443:24443 "$image"
fi
if [[ ! -s $state/token ]]; then
    phase=token
    issued=$(docker exec "$container" /app/x-ui setting -getApiToken -tokenName bot-managed -tokenScope admin)
    token=$(awk '/^apiToken: / {print $2}' <<<"$issued")
    [[ "$token" =~ ^[A-Za-z0-9_-]{24,}$ ]]
    printf '%s\n' "$token" > "$state/token"
fi
phase=subscription
sub_path=""
for _ in $(seq 1 15); do
    sub_path=$(curl -fsS --max-time 5 -X POST \
        -H "Authorization: Bearer $(cat "$state/token")" \
        "http://127.0.0.1:2053/$path/panel/api/setting/all" |
        python3 -c 'import json,sys; o=json.load(sys.stdin)["obj"]; print((json.loads(o) if isinstance(o,str) else o)["subPath"])') || true
    [[ "$sub_path" =~ ^(/[A-Za-z0-9_-]+)+/$ ]] && break
    sleep 2
done
[[ "$sub_path" =~ ^(/[A-Za-z0-9_-]+)+/$ ]]
[[ "$sub_path" != "/$path/" ]]
cat > "$state/Caddyfile" <<EOF
$http_block
https://$domain {
    $tls
    handle ${sub_path}* {
        reverse_proxy 127.0.0.1:2096
    }
    handle /$path/* {
        reverse_proxy 127.0.0.1:2053
    }
    handle {
        respond 404
    }
}
EOF
phase=https
caddy validate --config "$state/Caddyfile" --adapter caddyfile
install -m 0644 "$state/Caddyfile" /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
printf '%s\n' "$path" "$(cat "$state/token")" "$sub_path" >&3
