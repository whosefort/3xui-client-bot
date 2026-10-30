"""Пакет панелей: единый интерфейс + фабрика выбора бэкенда по конфигу."""
from __future__ import annotations

from ..config import config
from .base import Client, PanelClient, PanelError


def _build_one(kind: str) -> PanelClient:
    if kind == "marzban":
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


def build_panel() -> PanelClient:
    """PANELS=marzban,xui — несколько панелей, первая главная (bot/panels/multi.py).
    Без PANELS — одна панель по PANEL_BACKEND, как раньше."""
    kinds = config.panels
    if len(kinds) == 1:
        return _build_one(kinds[0])
    from .multi import MultiPanel
    return MultiPanel([(k, _build_one(k)) for k in kinds])


__all__ = ["Client", "PanelClient", "PanelError", "build_panel"]
