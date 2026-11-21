"""Manage explicit, per-client Xray routes without changing the default exit."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from .base import PanelError
from .xui import XUIClient


def outbound_tags(template: dict) -> list[str]:
    return [
        row["tag"] for row in template.get("outbounds", [])
        if row.get("tag") and row.get("protocol") != "dns"
    ]


async def exit_outbound(panel: XUIClient, key: str) -> dict:
    if not panel.exit_client:
        raise PanelError("Для этой панели не выбран выделенный клиент выхода.")
    client = await panel.find_by_username(panel.exit_client)
    if client is None or client.access_status != "active":
        raise PanelError("Выделенный клиент выходной панели недоступен.")
    candidates = []
    for raw in await panel.client_links(panel.exit_client):
        url = urlsplit(raw)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if (url.scheme == "vless" and url.hostname and url.port and url.username
                and query.get("security") == "reality" and query.get("type") in ("raw", "tcp")
                and query.get("encryption", "none") == "none"
                and query.get("flow", "") in ("", "xtls-rprx-vision")
                and query.get("sni") and query.get("pbk")):
            candidates.append((url, query))
    if len(candidates) != 1:
        raise PanelError("Выделенный клиент должен иметь одну ссылку VLESS RAW/REALITY.")
    url, q = candidates[0]
    return {
        "tag": "exit-" + key,
        "protocol": "vless",
        "settings": {"vnext": [{"address": url.hostname, "port": url.port, "users": [
            {"id": url.username, "encryption": "none", "flow": q.get("flow", "")}
        ]}]},
        "streamSettings": {
            "network": "tcp", "security": "reality", "realitySettings": {
                "serverName": q["sni"], "fingerprint": q.get("fp", "chrome"),
                "password": q["pbk"], "shortId": q.get("sid", ""), "spiderX": q.get("spx", "/"),
            },
        },
    }


def client_route(rule: dict, inbound: str, email: str, tags: list[str]) -> bool:
    # More specific routes (ports, destination ranges, multiple inbounds) are
    # edited in 3x-ui. The bot only manages routes covering one client's traffic.
    return (
        rule.get("type") == "field"
        and rule.get("enabled", True)
        and not rule.keys() - {"type", "inboundTag", "user", "outboundTag", "ruleTag", "enabled"}
        and rule.get("inboundTag") == [inbound]
        and isinstance(rule.get("user"), list) and email in rule["user"]
        and rule.get("outboundTag") in tags
    )


def route_client(template: dict, inbound: str, email: str, target: str | None) -> dict:
    changed = deepcopy(template)
    routing = changed.setdefault("routing", {})
    if routing.get("script"):
        raise PanelError("На панели включён скрипт маршрутизации. Правку нужно выполнить в 3x-ui.")
    rules = routing.setdefault("rules", [])
    tags = outbound_tags(template)
    if target is not None and target not in tags:
        raise PanelError("Выбранный выход больше не доступен.")
    assigned = [r for r in rules if client_route(r, inbound, email, tags)]
    if target is None and not assigned:
        return deepcopy(template)
    if len(assigned) == 1 and assigned[0]["outboundTag"] == target:
        return changed
    for rule in assigned:
        rule["user"] = [user for user in rule["user"] if user != email]
        if not rule["user"]:
            rules.remove(rule)
    if target is not None:
        protected = {"api"} | {
            out.get("tag") for out in changed.get("outbounds", [])
            if out.get("protocol") == "blackhole"
        }
        position = max((i + 1 for i, r in enumerate(rules) if r.get("enabled", True) and r.get("outboundTag") in protected),
                       default=0)
        rules.insert(position, {"type": "field", "inboundTag": [inbound],
                                "user": [email], "outboundTag": target})
    return changed


def _active_rules(config: dict) -> list[dict]:
    # 3x-ui strips the UI enable flag and omits disabled rules in its live config.
    return [{k: v for k, v in row.items() if k != "enabled"}
            for row in config.get("routing", {}).get("rules", []) if row.get("enabled", True)]


async def _wait_for_routes(panel: XUIClient, expected: dict) -> None:
    for _ in range(8):
        live = await panel.live_xray_config()
        status = (await panel.server_status()).get("xray", {})
        live_tags = {o.get("tag") for o in live.get("outbounds", [])}
        expected_tags = {o.get("tag") for o in expected.get("outbounds", [])}
        if _active_rules(live) == _active_rules(expected) and expected_tags <= live_tags and (
            str(status.get("state", "")).lower() == "running"
        ):
            return
        await asyncio.sleep(1)
    raise PanelError("Xray не подтвердил применение правил.")


async def set_client_route(panel: XUIClient, inbound_id: int, email: str, target: str | None,
                           *, outbound: dict | None = None) -> bool:
    async with panel.config_lock:
        inbound = next((row for row in await panel.list_inbounds() if row["id"] == inbound_id), None)
        if not inbound or not inbound.get("tag") or not any(
            client.get("email") == email for client in inbound["settings"].get("clients", [])
        ):
            raise PanelError("Клиент больше не привязан к этому входу.")
        original, test_url = await panel.xray_template()
        draft = deepcopy(original)
        if outbound is not None:
            rows = draft.setdefault("outbounds", [])
            existing = next((row for row in rows if row.get("tag") == outbound["tag"]), None)
            if existing is None:
                rows.append(outbound)
            elif existing != outbound:
                raise PanelError("Выход с этим тегом уже настроен иначе. Проверьте его в 3x-ui.")
        proposed = route_client(draft, inbound["tag"], email, target)
        if original == proposed:
            return False
        await commit_template(panel, original, proposed, test_url)
        return True


async def commit_template(panel: XUIClient, original: dict, proposed: dict, test_url: str | None) -> None:
    """Save and apply a template while the caller holds panel.config_lock."""
    try:
        await panel.save_xray_template(proposed, test_url)
        saved, _ = await panel.xray_template()
        if saved != proposed:
            raise PanelError("3x-ui сохранила шаблон, отличающийся от ожидаемого.")
        await panel.restart_xray()
        await _wait_for_routes(panel, proposed)
    except Exception as exc:
        try:
            current, current_url = await panel.xray_template()
            if current not in (original, proposed):
                raise PanelError("Шаблон изменился параллельно.")
            if current == proposed:
                if current_url != test_url:
                    raise PanelError("Настройки изменились параллельно.")
                await panel.save_xray_template(original, test_url)
                await panel.restart_xray()
            await _wait_for_routes(panel, original)
        except Exception as rollback:
            raise PanelError("Правка не подтверждена. Проверьте состояние и откат в 3x-ui.") from rollback
        raise PanelError("Правка не применилась. Прежние правила проверены.") from exc
