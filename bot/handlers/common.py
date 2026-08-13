"""Общие хелперы для хендлеров: цена/реквизиты, распознавание клиента, уведомления."""
from __future__ import annotations

import logging
from typing import Optional

from .. import db
from ..config import config
from ..panels.base import Client
from ..runtime import get_bot, get_panel

log = logging.getLogger("handlers")


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
    panel = get_panel()
    user = db.get_user(tg_id)
    username = user["client_email"] if user else None

    client = (await panel.find_by_username(username)) if username else None
    if client is None:
        client = await panel.find_by_tgid(tg_id)
    if client is None:
        return None

    db.upsert_user(tg_id, user["tg_username"] if user else None,
                   client_email=client.username, sub_id=client.sub_url)
    return client


async def notify_admins(text: str, reply_markup=None) -> None:
    bot = get_bot()
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог уведомить админа %s: %s", admin_id, e)
