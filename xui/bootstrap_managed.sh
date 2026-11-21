#!/usr/bin/env bash
# This script is streamed over authenticated SSH. Its stdout is a private protocol.
set -euo pipefail
umask 077
domain=$1
image=$2
[[ "$domain" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ ]]
[[ "$image" =~ ^ghcr\.io/mhsanaei/3x-ui@sha256:[0-9a-f]{64}$ ]]
[[ $(id -u) == 0 ]]

state=/opt/bot-managed-3xui
container=bot-managed-3xui
exec 3>&1
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
if ! docker container inspect "$container" >/dev/null 2>&1; then
    docker pull "$image"
    docker run --rm --entrypoint /app/x-ui -v "$state/db:/etc/x-ui" "$image" \
        setting -username admin -password "$(cat "$state/admin-password")" -port 2053 -webBasePath "/$path/"
    docker run -d --name "$container" --restart unless-stopped \
        -e XUI_ENABLE_FAIL2BAN=false -v "$state/db:/etc/x-ui" \
        -p 127.0.0.1:2053:2053 -p 127.0.0.1:2096:2096 -p 24443:24443 "$image"
fi
if [[ ! -s $state/token ]]; then
    issued=$(docker exec "$container" /app/x-ui setting -getApiToken -tokenName bot-managed -tokenScope admin)
    token=$(awk '/^apiToken: / {print $2}' <<<"$issued")
    [[ "$token" =~ ^[A-Za-z0-9_-]{24,}$ ]]
    printf '%s\n' "$token" > "$state/token"
fi
cat > "$state/Caddyfile" <<EOF
$domain {
    handle /sub/* {
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
caddy validate --config "$state/Caddyfile" --adapter caddyfile
install -m 0644 "$state/Caddyfile" /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
printf '%s\n' "$path" "$(cat "$state/token")" >&3
