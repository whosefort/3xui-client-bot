#!/usr/bin/env bash
#
# Быстрый bootstrap нового сервера под 3x-ui.
#   - переносит SSH на порт 2222 (с обработкой socket-активации Ubuntu 24.04)
#   - ставит и настраивает UFW (в безопасном порядке, без лок-аута)
#   - открывает нужные порты (в т.ч. опция "порт только для Cloudflare")
#   - ставит 3x-ui
#   - включает BBR (ускоряет VPN)
#
# Запуск на СВЕЖЕМ сервере под root:
#   curl -fsSLO <url>/bootstrap-server.sh   # или scp этот файл
#   bash bootstrap-server.sh
#
# ⚠️ После смены SSH-порта НЕ закрывай текущую сессию, пока не проверишь вход по 2222
#    в новом окне:  ssh -p 2222 root@IP
# =============================================================================

set -euo pipefail

# ------------------------- КОНФИГ (правь под себя) ---------------------------
SSH_PORT=2222

# Порты, открытые ВСЕМ (tcp):
OPEN_PORTS_ANY=(443 80)

# Порты, открытые ТОЛЬКО для диапазонов Cloudflare (origin для WS/CDN-инбаунда).
# Оставь пустым () если CDN-инбаунд не нужен.
CF_ONLY_PORTS=(8443)

# Если знаешь порты панели/подписки 3x-ui заранее — впиши, откроются автоматически.
# (Обычно узнаёшь ПОСЛЕ установки: `x-ui settings`. Тогда открой вручную — см. хвост скрипта.)
PANEL_PORT=""
SUB_PORT=""

INSTALL_3XUI=true
ENABLE_BBR=true
# -----------------------------------------------------------------------------

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запускай под root."
command -v apt-get >/dev/null 2>&1 || die "Скрипт для Debian/Ubuntu (apt)."

export DEBIAN_FRONTEND=noninteractive

log "Обновляю пакеты и ставлю зависимости…"
apt-get update -y
apt-get install -y ufw curl wget socat cron

# ============================ 1. SSH -> 2222 =================================
log "Переношу SSH на порт ${SSH_PORT}…"

# закомментировать активные 'Port ...' в основном конфиге, чтобы не было конфликта
sed -ri 's/^[[:space:]]*Port[[:space:]]+[0-9]+/#&/' /etc/ssh/sshd_config || true

# drop-in с новым портом (поддерживается современным OpenSSH)
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-port.conf <<EOF
Port ${SSH_PORT}
EOF

# Ubuntu 22.10+/24.04: SSH стартует через socket-активацию и ИГНОРИРУЕТ Port в sshd_config.
# Если socket есть — отключаем его и переходим на обычный ssh.service.
if systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.socket'; then
  warn "Обнаружена socket-активация SSH (Ubuntu 24.04) — отключаю, иначе порт не сменится."
  systemctl disable --now ssh.socket 2>/dev/null || true
  systemctl mask ssh.socket 2>/dev/null || true
fi

# имя сервиса: ssh (Debian/Ubuntu) или sshd
SSH_SVC="ssh"; systemctl list-unit-files 2>/dev/null | grep -q '^sshd\.service' && SSH_SVC="sshd"
systemctl enable "$SSH_SVC" 2>/dev/null || true

# проверить конфиг перед рестартом
sshd -t || die "Ошибка в sshd_config — SSH не тронут, разберись перед рестартом."

# ============================ 2. UFW ========================================
# ВАЖНО: сначала правила (в т.ч. 2222), потом enable. UFW пропускает established,
# поэтому текущая сессия не рвётся.
log "Настраиваю UFW…"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing

ufw allow "${SSH_PORT}/tcp" comment 'SSH'
for p in "${OPEN_PORTS_ANY[@]}"; do
  [ -n "$p" ] && ufw allow "${p}/tcp" comment 'open-any'
done
[ -n "$PANEL_PORT" ] && ufw allow "${PANEL_PORT}/tcp" comment '3xui-panel'
[ -n "$SUB_PORT" ]   && ufw allow "${SUB_PORT}/tcp"   comment '3xui-sub'

# --- порты только для Cloudflare ---
if [ "${#CF_ONLY_PORTS[@]}" -gt 0 ]; then
  log "Открываю ${CF_ONLY_PORTS[*]} только для диапазонов Cloudflare…"
  CF4="$(curl -fsSL https://www.cloudflare.com/ips-v4 2>/dev/null || true)"
  CF6="$(curl -fsSL https://www.cloudflare.com/ips-v6 2>/dev/null || true)"
  # fallback на зашитый список, если не скачалось
  if [ -z "$CF4" ]; then
    CF4="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22"
  fi
  [ -z "$CF6" ] && CF6="2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"
  for port in "${CF_ONLY_PORTS[@]}"; do
    for ip in $CF4 $CF6; do
      ufw allow proto tcp from "$ip" to any port "$port" comment 'CF-origin' >/dev/null
    done
  done
fi

ufw --force enable
log "UFW включён."

# рестарт SSH уже под защитой UFW (порт 2222 разрешён)
systemctl restart "$SSH_SVC" || systemctl restart ssh || die "Не смог перезапустить SSH."
log "SSH слушает порт ${SSH_PORT}."

# ============================ 3. BBR ========================================
if [ "$ENABLE_BBR" = true ]; then
  log "Включаю BBR + fq…"
  cat > /etc/sysctl.d/99-bbr.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
  sysctl --system >/dev/null 2>&1 || true
fi

# ============================ 4. 3x-ui ======================================
if [ "$INSTALL_3XUI" = true ]; then
  log "Ставлю 3x-ui (официальный установщик MHSanaei)…"
  warn "Установщик может задать пару вопросов (порт панели, логин/пароль) — отвечай."
  bash <(curl -Ls https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh) || \
    warn "Установщик 3x-ui завершился с ошибкой — поставь вручную и открой порт панели."
fi

# ============================ Итог ==========================================
echo
log "ГОТОВО. Текущее состояние UFW:"
ufw status verbose | sed 's/^/    /'
echo
warn "ПРОВЕРЬ ВХОД: в НОВОМ окне ->  ssh -p ${SSH_PORT} root@<IP>"
warn "Пока не убедился — НЕ закрывай эту сессию."
echo
cat <<TIP
Дальше:
  1) Узнай порты панели/подписки:   x-ui settings
  2) Если не открыл заранее — открой их:
        ufw allow <PANEL_PORT>/tcp comment '3xui-panel'
        ufw allow <SUB_PORT>/tcp   comment '3xui-sub'
  3) REALITY-инбаунд вешай на 443 (уже открыт).
  4) WS/CDN-инбаунд (если нужен) — на 8443 (открыт только для Cloudflare).
     Настройку CF-фронта смотри в cloudflare-setup.md.
TIP
