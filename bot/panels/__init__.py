"""Пакет панелей: единый интерфейс + фабрика выбора бэкенда по конфигу."""
from __future__ import annotations

from ..config import config
from .base import Client, PanelClient, PanelError


def build_panel() -> PanelClient:
    """Собрать бэкенд по PANEL_BACKEND (xui | marzban)."""
    if config.panel_backend == "marzban":
        from .marzban import MarzbanClient
        return MarzbanClient(
            config.marzban_url, config.marzban_username, config.marzban_password,
            reset_strategy=config.marzban_reset_strategy,
            proxies=config.marzban_proxies or None,
            inbounds=config.marzban_inbounds or None,
        )
    from .xui import XUIClient
    return XUIClient(
        config.xui_base_url, auth=config.xui_auth, api_token=config.xui_api_token,
        username=config.xui_username, password=config.xui_password,
        twofa_secret=config.xui_2fa_secret, client_flow=config.client_flow,
        sub_url_template=config.sub_url_template, inbound_ids=config.default_inbound_ids,
    )


__all__ = ["Client", "PanelClient", "PanelError", "build_panel"]
