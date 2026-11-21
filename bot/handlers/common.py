"""Общие хелперы для хендлеров: цена/реквизиты, распознавание клиента, уведомления."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .. import db
from ..config import config
from ..panels.base import Client, PanelClient, PanelError
from ..runtime import get_bot, get_panel, panel_for, panel_key_for

log = logging.getLogger("handlers")
binding_lock = asyncio.Lock()


class BindingConflict(Exception):
    pass


async def bind_client(panel: PanelClient, client: Client, tg_id: int,
                      tg_username: str | None = None) -> None:
    """Под `binding_lock` проверить получателя и сохранить привязку."""
    key = panel_key_for(panel)
    existing = db.get_user(tg_id)
    if existing and existing["client_email"] and existing["panel_key"] != key:
        raise BindingConflict("У этого Telegram-аккаунта уже есть подписка на другой панели.")
    owner = await panel.find_by_tgid(tg_id)
    if owner and owner.username != client.username:
        raise BindingConflict("У этого Telegram-аккаунта уже есть другая подписка.")
    await panel.bind_tgid(client=client, tg_id=tg_id)
    db.save_client_binding(tg_id, tg_username, client.username, client.sub_url, key)


def get_price() -> str:
    return db.get_setting("price", config.default_price)


def get_requisites() -> str:
    return db.get_setting("requisites", config.default_requisites)


def get_traffic_gb() -> int:
    """Месячный лимит трафика (ГБ). Редактируется из админки (БД), иначе из .env."""
    raw = db.get_setting("traffic_gb", str(config.plan_traffic_gb))
    try:
        v = int(raw)
        return v if v > 0 else config.plan_traffic_gb
    except (TypeError, ValueError):
        return config.plan_traffic_gb


async def resolve_client(tg_id: int) -> Optional[Client]:
    """Вернуть нормализованного Client из панели или None.

    Источник правды — панель. username кэшируем в БД (client_email): при наличии
    кэша ищем по username, иначе по tgId.
    """
    async with binding_lock:
        user = db.get_user(tg_id)
        key = user["panel_key"] if user else ""
        try:
            panel = panel_for(key) if key else get_panel()
        except PanelError:
            log.warning("Панель подписки %s не подключена", key)
            return None
        username = user["client_email"] if user else None
        client = await panel.find_by_username(username) if username else None
        if not client or client.tg_id != tg_id:
            client = await panel.find_by_tgid(tg_id)
        if not client or client.tg_id != tg_id:
            return None
        db.upsert_user(tg_id, None, client_email=client.username, sub_id=client.sub_url)
        return client


async def notify_admins(text: str, reply_markup=None) -> None:
    bot = get_bot()
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог уведомить админа %s: %s", admin_id, e)
