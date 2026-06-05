"""Общие синглтоны, инициализируются в main.py при старте."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot

from .xui import XUIClient

xui: Optional[XUIClient] = None
bot: Optional[Bot] = None


def get_xui() -> XUIClient:
    assert xui is not None, "XUI клиент не инициализирован"
    return xui


def get_bot() -> Bot:
    assert bot is not None, "Bot не инициализирован"
    return bot
