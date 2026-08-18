#!/usr/bin/env bash
# Обёртка для cron: маппит имена переменных из .env бота (MARZBAN_*) на те,
# что ждёт check_xray_drift.py (PANEL_*), не дублируя креды в двух местах.
#
# НЕ делаем `source .env` — .env это KEY=value для python-dotenv, а не
# валидный bash: любое значение с пробелами без кавычек (например
# DEFAULT_REQUISITES=По запросу у контакта поддержки) bash пытается
# исполнить как отдельную команду и падает под set -e. Тянем построчно
# только нужные ключи.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

_env_get() {
    # Последнее совпадение (на случай повторного KEY= в файле), без
    # экранирования кавычек — значения тут не подставляются в шелл.
    grep -E "^${1}=" .env | tail -1 | cut -d= -f2-
}

export PANEL_URL="$(_env_get MARZBAN_URL)"
export PANEL_USER="$(_env_get MARZBAN_USERNAME)"
export PANEL_PASS="$(_env_get MARZBAN_PASSWORD)"
export XRAY_TARGET_VERSION="$(cat node/XRAY_VERSION)"

python3 node/check_xray_drift.py
