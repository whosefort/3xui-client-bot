"""Общие синглтоны, инициализируются в main.py при старте."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot

from .panels.base import PanelClient
from .panels.xui import XUIClient

panel: Optional[PanelClient] = None
bot: Optional[Bot] = None
xui_panels: dict[str, tuple[str, XUIClient]] = {}


def get_panel() -> PanelClient:
    assert panel is not None, "Панель не инициализирована"
    return panel


def get_bot() -> Bot:
    assert bot is not None, "Bot не инициализирован"
    return bot


def panel_for(key: str | None = None) -> PanelClient:
    if not key:
        return get_panel()
    from .panels.base import PanelError
    if key not in xui_panels:
        raise PanelError("Панель этой подписки сейчас не подключена.")
    return xui_panels[key][1]


def panel_key_for(panel: PanelClient) -> str:
    return next((key for key, (_, client) in xui_panels.items() if panel is client), "")
