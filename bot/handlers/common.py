"""Общие хелперы для хендлеров: цена/реквизиты, распознавание клиента, уведомления."""
from __future__ import annotations

import logging
from typing import Optional

from .. import db
from ..config import config
from ..runtime import get_bot, get_xui

log = logging.getLogger("handlers")


def get_price() -> str:
    return db.get_setting("price", config.default_price)


def get_requisites() -> str:
    return db.get_setting("requisites", config.default_requisites)


def sub_link(sub_id: str) -> str:
    return get_xui().sub_url(sub_id, template=config.sub_url_template)


async def resolve_client(tg_id: int) -> Optional[dict]:
    """Вернуть объект клиента из панели (expiryTime, enable, subId, …) или None.

    Источник правды — Clients API панели. Email/subId кэшируем в БД, чтобы при
    наличии кэша искать по email, иначе — по tgId. Сам объект содержит всё нужное.
    """
    xui = get_xui()
    user = db.get_user(tg_id)
    email = user["client_email"] if user else None

    client = (await xui.find_by_email(email)) if email else None
    if client is None:
        client = await xui.find_by_tgid(tg_id)
    if client is None:
        return None

    # синхронизируем кэш идентичности
    db.upsert_user(tg_id, user["tg_username"] if user else None,
                   client_email=client.get("email"), sub_id=client.get("subId"))
    return client


async def notify_admins(text: str, reply_markup=None) -> None:
    bot = get_bot()
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог уведомить админа %s: %s", admin_id, e)
