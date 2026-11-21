"""Named panel ownership and deterministic VLESS issuance."""
from __future__ import annotations

from urllib.parse import urlsplit

from .. import db, runtime
from ..config import config
from .base import Client, PanelClient, PanelError
from .xui import XUIClient


def owner_key(tg_id: int) -> str | None:
    record = db.get_user(tg_id)
    return record["panel_key"] if record and record["client_email"] else None


async def ensure_panel_owner(tg_id: int, key: str) -> None:
    owner = owner_key(tg_id)
    if owner is None and key and runtime.get_panel() is not runtime.panel_for(key):
        if await runtime.get_panel().find_by_tgid(tg_id):
            owner = ""
    if owner is not None and owner != key:
        raise PanelOwnerConflict("У этого Telegram-аккаунта уже есть подписка на другой панели.")


class PanelOwnerConflict(PanelError):
    pass


def subscription_targets() -> dict[str, tuple[str, XUIClient]]:
    primary = config.xui_base_url if config.uses("xui") else ""
    return {k: v for k, v in runtime.xui_panels.items()
            if v[1].sub_url_template and v[1].base != primary}


def share_route(url: str) -> tuple:
    parsed = urlsplit(url)
    base, _, _ = parsed.path.rstrip("/").rpartition("/")
    return parsed.scheme.lower(), parsed.hostname, parsed.port or 443, base


async def locate_subscription(value: str, sub_id: str) -> tuple[PanelClient, Client | None]:
    targets = subscription_targets()
    default = runtime.panel_for()
    if not targets:
        return default, await default.find_by_subid(sub_id)
    supplied = urlsplit(value.strip())
    if supplied.hostname:
        matching = [p for _, p in targets.values()
                    if share_route(value.strip()) == share_route(p.sub_url_template)]
        if len(matching) == 1:
            selected = matching[0]
            failure = None
            try:
                client = await selected.find_by_subid(sub_id)
            except (PanelError, OSError, TimeoutError) as exc:
                client = None
                failure = exc
            if client is None:
                legacy = await default.find_by_subid(sub_id)
                if legacy and legacy.sub_url and share_route(legacy.sub_url) == share_route(value.strip()):
                    return default, legacy
            if failure:
                raise failure
            return selected, client
        if matching:
            raise PanelError("Адрес подписки совпадает у нескольких панелей.")
        return default, await default.find_by_subid(sub_id)
    legacy = await default.find_by_subid(sub_id)
    matches = [(default, legacy)] if legacy else []
    failed = None
    for _, panel in targets.values():
        if panel is default:
            continue
        try:
            client = await panel.find_by_subid(sub_id)
        except (PanelError, OSError, TimeoutError) as exc:
            failed = exc
            continue
        if client:
            # The existing single 3x-ui can also be in the management menu.
            if not any(c.sub_url and c.sub_url == client.sub_url for _, c in matches):
                matches.append((panel, client))
    if len(matches) > 1:
        raise PanelError("Код совпал на нескольких панелях. Пришлите полную ссылку.")
    if failed and not legacy:
        raise failed
    return matches[0] if matches else (default, None)


async def issuance_inbounds(panel: XUIClient) -> list[dict]:
    rows = [r for r in await panel.list_inbounds() if r.get("enable") and r.get("protocol") == "vless"]
    return sorted(rows, key=lambda r: (r.get("excludeFromSub", False), r["id"]))


async def create_named_client(panel: XUIClient, tg_id: int, days: int, gb: int,
                              inbound_id: int | None, *, username: str | None = None) -> Client:
    rows = [r for r in await issuance_inbounds(panel) if not r.get("excludeFromSub")]
    inbound = next((row for row in rows if inbound_id is None or row["id"] == inbound_id), None)
    if inbound is None:
        raise PanelError("На панели нет доступного VLESS-входа для подписки.")
    stream = inbound["streamSettings"]
    vision = (stream.get("network") in ("tcp", "raw") and stream.get("security") in ("tls", "reality")
              and not inbound.get("disableFlow"))
    return await panel.create_client(tg_id=tg_id, days=days, traffic_gb=gb, inbound_id=inbound["id"],
                                     username=username,
                                     flow="xtls-rprx-vision" if vision else "")
