"""3x-ui infrastructure operations and version-compatible presets."""
from __future__ import annotations

import asyncio
import json
import secrets
from copy import deepcopy
from urllib.parse import urlsplit

from .base import PanelError
from .xui import XUIClient
from .xui_routes import commit_template

REALITY_PRESETS = {
    "github": "github.com",
    "cisco": "www.cisco.com",
    "google": "dl.google.com",
}
OUTBOUND_PRESETS = {
    "ipv4": {"tag": "bot-ipv4", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}},
    "direct": {"tag": "bot-direct", "protocol": "freedom", "settings": {}},
    "block": {"tag": "bot-blocked", "protocol": "blackhole", "settings": {}},
}


async def list_nodes(panel: XUIClient) -> list[dict]:
    response = await panel._request("GET", "/panel/api/nodes/list")
    return panel._object(response.get("obj")) or []


async def probe_node(panel: XUIClient, node_id: int) -> dict:
    await panel._request("POST", f"/panel/api/nodes/probe/{node_id}", form={})
    result = await panel._request("GET", f"/panel/api/nodes/get/{node_id}")
    return panel._object(result.get("obj")) or {}


async def enable_node(panel: XUIClient, node_id: int, enabled: bool) -> dict:
    await panel._request("POST", f"/panel/api/nodes/setEnable/{node_id}", json_body={"enable": enabled})
    result = await panel._request("GET", f"/panel/api/nodes/get/{node_id}")
    node = panel._object(result.get("obj")) or {}
    if node.get("enable") is not enabled:
        raise PanelError("3x-ui не подтвердила изменение ноды.")
    return node


async def remove_node(panel: XUIClient, node_id: int) -> None:
    async with panel.config_lock:
        ids = {n["id"] for n in await list_nodes(panel)}
        if node_id not in ids:
            return
        await panel._request("POST", f"/panel/api/nodes/del/{node_id}", form={})
        if any(n["id"] == node_id for n in await list_nodes(panel)):
            raise PanelError("3x-ui не подтвердила удаление ноды.")


async def enable_inbound(panel: XUIClient, inbound_id: int, enabled: bool) -> None:
    async with panel.config_lock:
        existing = next((r for r in await panel.list_inbounds() if r["id"] == inbound_id), None)
        if not existing:
            raise PanelError("Вход больше не существует.")
        if existing.get("enable") is enabled:
            return
        await panel._request("POST", f"/panel/api/inbounds/setEnable/{inbound_id}",
                             json_body={"enable": enabled})
        if not any(r["id"] == inbound_id and r.get("enable") is enabled
                   for r in await panel.list_inbounds()):
            raise PanelError("3x-ui не подтвердила состояние входа.")


async def add_node(panel: XUIClient, other: XUIClient, name: str) -> dict:
    if panel.base == other.base or not other.node_token:
        raise PanelError("Выберите отдельную панель с токеном для связи нод.")
    url = urlsplit(other.base)
    body = {"name": name, "scheme": "https", "address": url.hostname, "port": url.port or 443,
            "basePath": url.path or "/", "apiToken": other.node_token, "allowPrivateAddress": False}
    async with panel.config_lock:
        current = await list_nodes(panel)
        for node in current:
            if (node.get("address"), node.get("port"), (node.get("basePath") or "/").rstrip("/")) == (
                body["address"], body["port"], body["basePath"].rstrip("/")
            ):
                return node
        tested = await panel._request("POST", "/panel/api/nodes/test", json_body=body)
        if (panel._object(tested.get("obj")) or {}).get("status") != "online":
            raise PanelError("3x-ui не подтвердила доступ к API новой ноды.")
        result = await panel._request("POST", "/panel/api/nodes/add", json_body={**body, "enable": True})
        node = panel._object(result.get("obj")) or {}
        if not node.get("id"):
            raise PanelError("3x-ui не вернула идентификатор ноды. Проверьте список перед повтором.")
        return node


async def test_outbound(panel: XUIClient, tag: str) -> dict:
    template, _ = await panel.xray_template()
    rows = template.get("outbounds", [])
    outbound = next((o for o in rows if o.get("tag") == tag), None)
    if outbound is None or outbound.get("protocol") in ("blackhole", "dns"):
        raise PanelError("Этот выход не подходит для проверки HTTP.")
    response = await panel._request("POST", "/panel/api/xray/testOutbound", form={
        "mode": "real", "outbound": json.dumps(outbound), "allOutbounds": json.dumps(rows),
    })
    result = panel._object(response.get("obj"))
    if not isinstance(result, dict):
        raise PanelError("3x-ui не вернула результат проверки выхода.")
    return result


async def add_outbound_preset(panel: XUIClient, preset: str) -> bool:
    new = OUTBOUND_PRESETS[preset]
    async with panel.config_lock:
        original, url = await panel.xray_template()
        changed = deepcopy(original)
        rows = changed.get("outbounds") or []
        if not rows:
            raise PanelError("У шаблона нет штатного выхода. Сначала проверьте 3x-ui.")
        existing = next((o for o in rows if o.get("tag") == new["tag"]), None)
        if existing is not None:
            if existing != new:
                raise PanelError("Тег пресета уже занят другой конфигурацией.")
            return False
        rows.append(deepcopy(new))
        await commit_template(panel, original, changed, url)
    return True


async def _candidate(panel: XUIClient, hostname: str) -> str | None:
    try:
        result = await panel._request("POST", "/panel/api/server/scanRealityTarget",
                                      form={"target": hostname + ":443", "sni": hostname, "xver": "0"})
    except PanelError:
        return None
    row = panel._object(result.get("obj")) or {}
    if row.get("feasible") and row.get("certValid") and row.get("h2") and row.get("tls13"):
        return hostname
    return None


async def pick_reality_target(panel: XUIClient, preset: str) -> str:
    choices = (list(REALITY_PRESETS.values()) if preset == "auto" else [REALITY_PRESETS[preset]])
    checks = await asyncio.gather(*(_candidate(panel, host) for host in choices))
    host = next((result for result in checks if result), None)
    if not host:
        raise PanelError("3x-ui не нашла пригодную цель REALITY среди выбранных пресетов.")
    return host


async def add_reality_preset(panel: XUIClient, preset: str) -> dict:
    async with panel.config_lock:
        existing = await panel.list_inbounds()
        label = "VLESS REALITY · " + preset
        previous = next((row for row in existing if row.get("remark") == label), None)
        if previous is not None:
            stream = previous.get("streamSettings", {})
            if previous.get("protocol") != "vless" or stream.get("security") != "reality":
                raise PanelError("Название пресета уже занято другим входом.")
            return previous
        host = await pick_reality_target(panel, preset)
        used = {row.get("port") for row in existing}
        port = next((n for n in range(24443, 24463) if n not in used), None)
        if port is None:
            raise PanelError("В пуле пресетов нет свободных портов.")
        keys = panel._object((await panel._request("GET", "/panel/api/server/getNewX25519Cert")).get("obj"))
        if not isinstance(keys, dict) or not keys.get("privateKey") or not keys.get("publicKey"):
            raise PanelError("3x-ui не вернула пару ключей REALITY.")
        response = await panel._request("POST", "/panel/api/inbounds/add", json_body={
            "remark": label, "listen": "", "port": port, "enable": True, "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
                "target": host + ":443", "serverNames": [host], "xver": 0,
                "privateKey": keys["privateKey"], "shortIds": [secrets.token_hex(8)],
                "settings": {"publicKey": keys["publicKey"], "fingerprint": "chrome"},
            }},
            "sniffing": {"enabled": False},
        })
        new = panel._object(response.get("obj")) or {}
        installed = next((row for row in await panel.list_inbounds()
                          if row.get("port") == port and row.get("remark") == label), None)
        if installed is None or (new.get("id") and new["id"] != installed["id"]):
            raise PanelError("Вход не подтверждён. Проверьте список перед повторной попыткой.")
        return installed


async def set_reality_target(panel: XUIClient, inbound_id: int, preset: str) -> str:
    async with panel.config_lock:
        endpoint = f"/panel/api/inbounds/get/{inbound_id}"
        current = panel._object((await panel._request("GET", endpoint)).get("obj")) or {}
        original = panel._object(current.get("streamSettings")) or {}
        reality = original.get("realitySettings") or {}
        if (current.get("protocol") != "vless" or original.get("security") != "reality"
                or original.get("network") not in ("raw", "tcp") or reality.get("xver", 0) != 0):
            raise PanelError("Пресет подходит только для VLESS RAW/REALITY с Xver 0.")
        host = await pick_reality_target(panel, preset)
        requested = deepcopy(original)
        target = requested["realitySettings"]
        fields = [name for name in ("dest", "target") if name in target] or ["target"]
        for field in fields:
            target[field] = host + ":443"
        target["serverNames"] = [host]
        if requested == original:
            return host
        payload = {k: v for k, v in current.items() if k not in ("userId", "clientStats")}
        path = f"/panel/api/inbounds/update/{inbound_id}"

        async def read_stream() -> dict:
            reply = panel._object((await panel._request("GET", endpoint)).get("obj")) or {}
            return panel._object(reply.get("streamSettings")) or {}

        try:
            await panel._request("POST", path, json_body={**payload, "streamSettings": requested})
            if await read_stream() != requested:
                raise PanelError("Панель не подтвердила изменение REALITY.")
        except Exception as exc:
            try:
                latest = await read_stream()
                if latest == requested:
                    snapshot = panel._object((await panel._request("GET", endpoint)).get("obj")) or {}
                    if panel._object(snapshot.get("streamSettings")) != requested:
                        raise PanelError("Настройки изменились параллельно.")
                    rollback = {k: v for k, v in snapshot.items() if k not in ("userId", "clientStats")}
                    await panel._request("POST", path, json_body={**rollback, "streamSettings": original})
                if await read_stream() != original:
                    raise PanelError("Предыдущий вход не подтверждён.")
            except Exception as rollback_error:
                raise PanelError("Настройка не подтверждена. Проверьте вход в 3x-ui.") from rollback_error
            raise PanelError("Ошибка изменения. Прежний вход сохранён.") from exc
        return host
