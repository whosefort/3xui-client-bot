#!/usr/bin/env python3
"""check_xray_drift.py — сверяет xray-core на нодах с последним тегом XTLS,
шлёт админам в Telegram, если нода отстала.

Зачем: клиентские приложения (Happ, v2rayNG и т.п.) сами обновляют свой
xray-core и часто следят за pre-release тегами. Ноды — нет. Рассинхрон рвёт
REALITY-хендшейк тихо: панель показывает "connected", а живые клиенты не
пингуются. См. TROUBLESHOOTING.md, раздел #1 — это была причина инцидента
на весь день. Этот скрипт ловит дрейф ДО жалоб от клиентов, не после.

Не требует SSH до нод — версия каждой ноды уже приходит в /api/nodes
(поле xray_version), панель сама её знает через node-протокол.

Сверяет не с абсолютным "latest" на GitHub (там pre-release тег выходит
примерно раз в неделю — алерт на каждый такой тег превратится в шум, который
через месяц никто не читает), а с XRAY_TARGET_VERSION — версией, которую
кто-то реально проверил живым клиентом и зафиксировал в node/XRAY_VERSION.
Алерт значит "нода откатилась НИЖЕ уже проверенной версии" (например,
контейнер пересоздали и патч-бинарник слетел) — это всегда достойно внимания.
GitHub при этом всё равно проверяется — если там появилось что-то новее
зафиксированного пина, это печатается в лог informational-строкой, без пинга
админа: обновление пина — осознанное решение (проверить новую версию с живым
клиентом, обновить node/XRAY_VERSION), не автоматика.

Переменные окружения (совпадают по именам с bot/.env, специально):
  PANEL_URL, PANEL_USER, PANEL_PASS   — админ-доступ к панели
  BOT_TOKEN                           — токен телеграм-бота (для алерта)
  ADMIN_IDS                           — csv id админов, кому слать
  XRAY_TARGET_VERSION                 — пин из node/XRAY_VERSION (напр. v26.7.11).
                                         Не задан — сверка идёт с GitHub latest,
                                         но тогда жди шумных алертов каждую неделю.

Запуск руками:
  PANEL_URL=... PANEL_USER=... PANEL_PASS=... BOT_TOKEN=... ADMIN_IDS=123,456 \
    XRAY_TARGET_VERSION=$(cat XRAY_VERSION) python3 check_xray_drift.py

В кроне (раз в день, например в 09:00):
  0 9 * * * cd /path/to/repo && \
    PANEL_URL=... PANEL_USER=... PANEL_PASS=... BOT_TOKEN=... ADMIN_IDS=... \
    XRAY_TARGET_VERSION=$(cat node/XRAY_VERSION) \
    python3 node/check_xray_drift.py >> /var/log/xray_drift.log 2>&1
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _req(req: urllib.request.Request, timeout: int = 30):
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def panel_login(base: str, user: str, password: str) -> str:
    data = urllib.parse.urlencode({"username": user, "password": password}).encode()
    req = urllib.request.Request(base + "/api/admin/token", data=data, method="POST")
    return _req(req)["access_token"]


def panel_nodes(base: str, token: str) -> list[dict]:
    req = urllib.request.Request(base + "/api/nodes")
    req.add_header("Authorization", "Bearer " + token)
    return _req(req)


def latest_xray_tag() -> str:
    req = urllib.request.Request("https://api.github.com/repos/XTLS/Xray-core/releases?per_page=5")
    releases = _req(req)
    return releases[0]["tag_name"]


def ver_tuple(tag: str) -> tuple[int, ...]:
    # "v26.7.11" / "26.7.11" -> (26, 7, 11); мусор -> (0,) чтобы не падать
    s = tag.lstrip("vV")
    try:
        return tuple(int(p) for p in s.split("."))
    except ValueError:
        return (0,)


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage", data=data, method="POST"
    )
    try:
        _req(req)
    except urllib.error.HTTPError as e:
        print(f"не смог отправить в Telegram ({chat_id}): {e.read().decode()[:200]}", file=sys.stderr)


def main() -> int:
    panel_url = os.environ.get("PANEL_URL", "").rstrip("/")
    panel_user = os.environ.get("PANEL_USER", "")
    panel_pass = os.environ.get("PANEL_PASS", "")
    bot_token = os.environ.get("BOT_TOKEN", "")
    admin_ids = [a.strip() for a in os.environ.get("ADMIN_IDS", "").split(",") if a.strip()]
    target_version = os.environ.get("XRAY_TARGET_VERSION", "")

    missing = [n for n, v in (("PANEL_URL", panel_url), ("PANEL_USER", panel_user), ("PANEL_PASS", panel_pass)) if not v]
    if missing:
        print(f"не заданы переменные: {', '.join(missing)}", file=sys.stderr)
        return 2

    token = panel_login(panel_url, panel_user, panel_pass)
    nodes = panel_nodes(panel_url, token)
    latest = latest_xray_tag()

    if not target_version:
        print("XRAY_TARGET_VERSION не задан — сверяюсь с GitHub latest напрямую "
              "(будут шумные алерты примерно раз в неделю). Рекомендуется задать "
              "XRAY_TARGET_VERSION=$(cat node/XRAY_VERSION).", file=sys.stderr)
        target_version = latest
    target_v = ver_tuple(target_version)

    if ver_tuple(latest) > target_v:
        print(f"(инфо, не алерт) на GitHub есть новее пина: {latest} > {target_version}. "
              f"Проверь с живым клиентом и обнови node/XRAY_VERSION, если ок.")

    stale = []
    for n in nodes:
        node_v = ver_tuple(n.get("xray_version", ""))
        status = n.get("status")
        print(f"{n.get('name')} ({n.get('address')}): xray={n.get('xray_version')} status={status}")
        if status != "connected":
            continue  # своя проблема, не версия — не шумим тут
        if node_v < target_v:
            stale.append(n)

    if not stale:
        print(f"Все подключённые ноды на пинованной версии xray-core ({target_version}) или новее.")
        return 0

    lines = [f"⚠️ xray-core на {len(stale)} ноде(ах) откатился НИЖЕ проверенной версии {target_version}:"]
    for n in stale:
        lines.append(f"• {n.get('name')} ({n.get('address')}): {n.get('xray_version')}")
    lines.append("")
    lines.append("Обычно значит: контейнер ноды пересоздали и live-патч бинарника слетел.")
    lines.append("Почини: bash node/upgrade_xray.sh на каждой отставшей ноде.")
    lines.append("Симптом у клиентов, если не почини вовремя: REALITY 'received real certificate'.")
    text = "\n".join(lines)
    print(text)

    if bot_token and admin_ids:
        for chat_id in admin_ids:
            send_telegram(bot_token, chat_id, text)
    else:
        print("BOT_TOKEN/ADMIN_IDS не заданы — алерт не отправлен, только вывод в лог.", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
