#!/usr/bin/env bash
# Read-only диагностика conntrack на ноде (или мастере).
#
# Симптом: скорость VPN упала до единиц Мбит/с, в dmesg "table full".
# Причина и фикс: node/TROUBLESHOOTING.md, раздел
# «Просадка скорости у всех клиентов: nf_conntrack: table full».
#
# Запуск на ноде: bash node/check_conntrack.sh
set -u

max=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 0)
count=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)
timeout_est=$(cat /proc/sys/net/netfilter/nf_conntrack_tcp_timeout_established 2>/dev/null || echo "?")
echo "conntrack: ${count}/${max} (tcp_timeout_established=${timeout_est}s)"

pct=0
if [ "$max" -gt 0 ] 2>/dev/null; then
  pct=$(awk -v c="$count" -v m="$max" "BEGIN{printf \"%.1f\", 100*c/m}")
  echo "заполнение: ${pct}%"
fi

full=$(dmesg 2>/dev/null | grep -c "table full" || true)
last=$(dmesg -T 2>/dev/null | grep "table full" | tail -1 || true)
echo "table full в dmesg: ${full} шт. (последнее: ${last:-нет})"

echo
echo "Сокеты (если их в разы меньше conntrack_count — записи мёртвые):"
ss -s 2>/dev/null | head -6

echo
echo "SYN-SENT: $(ss -tan state syn-sent 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')"
echo "TIME-WAIT: $(ss -tan state time-wait 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')"
echo
netstat -s 2>/dev/null | grep -m1 -i "segments retransmited" || true

echo
if awk -v p="$pct" "BEGIN{exit !(p>=80)}"; then
  echo "⚠️  Таблица почти полная: ядро дропает пакеты новых соединений, у клиентов"
  echo "    падает скорость и зависают загрузки. Фикс — node/TROUBLESHOOTING.md,"
  echo "    раздел про conntrack (nf_conntrack_max + tcp_timeout_established)."
else
  echo "✅ Заполнение в норме (<80%)."
fi
