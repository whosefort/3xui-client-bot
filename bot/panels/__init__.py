"""Пакет панелей: единый интерфейс + фабрика выбора бэкенда по конфигу."""
from __future__ import annotations

from ..config import config
from .base import Client, PanelClient, PanelError
from .xui import XUIClient


def _build_one(kind: str) -> PanelClient:
    if kind == "marzban":
        from .marzban import MarzbanClient
        return MarzbanClient(
            config.marzban_url, config.marzban_username, config.marzban_password,
            reset_strategy=config.marzban_reset_strategy,
            proxies=config.marzban_proxies or None,
            inbounds=config.marzban_inbounds or None,
        )
    return XUIClient(
        config.xui_base_url, auth=config.xui_auth, api_token=config.xui_api_token,
        username=config.xui_username, password=config.xui_password,
        twofa_secret=config.xui_2fa_secret, client_flow=config.client_flow,
        sub_url_template=config.sub_url_template, inbound_ids=config.default_inbound_ids,
    )


def build_xui_management() -> dict[str, tuple[str, XUIClient]]:
    panels = {key: (name, XUIClient(url, api_token=token, exit_client=exit_client, sub_url_template=sub, node_token=node_token))
              for key, (name, url, token, exit_client, sub, node_token) in config.managed_xui().items()}
    if config.uses("xui") and not any(
        panel.base == config.xui_base_url for _, panel in panels.values()
    ):
        key = next((key for key in ("xui", "xui-default") if key not in panels), None)
        if key is None:
            raise RuntimeError("Освободите xui или xui-default для существующей 3x-ui.")
        panels[key] = ("3x-ui", _build_one("xui"))
    return panels


def build_panel() -> PanelClient:
    """PANELS=marzban,xui — несколько панелей, первая главная (bot/panels/multi.py).
    Без PANELS — одна панель по PANEL_BACKEND, как раньше."""
    kinds = config.panels
    if len(kinds) == 1 and not config.private_marzban_url:
        return _build_one(kinds[0])
    from .multi import MultiPanel
    private = None
    if config.private_marzban_url:
        from .marzban import MarzbanClient
        private = MarzbanClient(
            config.private_marzban_url, config.private_marzban_username,
            config.private_marzban_password, reset_strategy=config.marzban_reset_strategy,
            proxies={"vless": {"flow": "xtls-rprx-vision"}},
            inbounds={"vless": [config.private_marzban_inbound]},
        )
    return MultiPanel([(k, _build_one(k)) for k in kinds], private=private)


__all__ = ["Client", "PanelClient", "PanelError", "build_panel"]
