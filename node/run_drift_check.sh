#!/usr/bin/env bash
# Обёртка для cron: маппит имена переменных из .env бота (MARZBAN_*) на те,
# что ждёт check_xray_drift.py (PANEL_*), не дублируя креды в двух местах.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

export PANEL_URL="$MARZBAN_URL"
export PANEL_USER="$MARZBAN_USERNAME"
export PANEL_PASS="$MARZBAN_PASSWORD"
export XRAY_TARGET_VERSION="$(cat node/XRAY_VERSION)"

python3 node/check_xray_drift.py
