"""Independent monthly billing for private servers."""
from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError

from . import db, runtime
from .config import config

log = logging.getLogger(__name__)
lock = asyncio.Lock()


def now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Moscow"))


def next_month(due: date, billing_day: int) -> date:
    year = due.year + (due.month == 12)
    month = due.month % 12 + 1
    return date(year, month, min(billing_day, calendar.monthrange(year, month)[1]))


def display_date(value: str) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y")


async def _send(tg_id: int, message: str) -> bool:
    try:
        await runtime.get_bot().send_message(tg_id, message, parse_mode=None)
    except (TelegramAPIError, OSError, TimeoutError):
        log.warning("Не удалось отправить уведомление частного тарифа для tg_id=%s", tg_id)
        return False
    return True


async def _notify_payment(payment) -> bool:
    sent = await _send(payment["tg_id"],
                       f"Оплата получена · Частный сервер · {payment['server']}\n"
                       f"Получено: {payment['amount']} ₽\n"
                       f"Следующий платёж: {display_date(payment['next_due'])}.")
    if sent:
        db.mark_private_payment_notified(payment["billing_id"], payment["due"])
    return sent


async def notify_payment(billing_id: int, due: str) -> bool:
    payment = next((row for row in db.private_unnotified_payments()
                    if (row["billing_id"], row["due"]) == (billing_id, due)), None)
    return await _notify_payment(payment) if payment else True


async def sweep() -> None:
    async with lock:
        for payment in db.private_unnotified_payments():
            await _notify_payment(payment)
        current = now()
        if current.hour < config.remind_hour:
            return
        requisites = db.get_setting("requisites", config.default_requisites)
        for plan in db.private_billings():
            due = plan["due"]
            days = (date.fromisoformat(due) - current.date()).days
            if days not in (3, 1) and days > 0:
                continue
            kind = f"before_{days}" if days > 0 else "due"
            if (days > 0 and not plan["reminders"]) or db.private_event_sent(plan["id"], due, kind):
                continue
            heading = f"Напоминание · {'через 3 дня' if days == 3 else 'завтра'}" if days > 0 else "Платёж"
            if await _send(plan["tg_id"],
                           f"{heading} · Частный сервер · {plan['server']}\n"
                           f"Сумма: {plan['amount']} ₽\nДата платежа: {display_date(due)}\n\n"
                           f"Реквизиты для перевода\n{requisites}"):
                db.mark_private_event_sent(plan["id"], due, kind)
