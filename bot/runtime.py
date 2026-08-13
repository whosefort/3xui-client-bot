"""Общие синглтоны, инициализируются в main.py при старте."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot

from .panels.base import PanelClient

panel: Optional[PanelClient] = None
bot: Optional[Bot] = None


def get_panel() -> PanelClient:
    assert panel is not None, "Панель не инициализирована"
    return panel


def get_bot() -> Bot:
    assert bot is not None, "Bot не инициализирован"
    return bot
