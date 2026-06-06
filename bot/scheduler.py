"""Планировщик: ежедневные напоминания об окончании подписки + heartbeat админу.

Источник правды о сроке — 3X-UI (читаем на лету). Дедуп — таблица reminders_log:
одно напоминание на (клиент, дата конца подписки, порог дней). Если бот лежал и
пропустил точный день — на следующем запуске «догонит» за счёт условия days<=bucket.
"""
from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, texts
from .config import config
from .runtime import get_bot, get_xui

log = logging.getLogger("scheduler")


async def reminders_sweep() -> None:
    xui = get_xui()
    bot = get_bot()
    buckets = sorted(config.remind_days_before, reverse=True)  # напр. [3,1,0]
    checked = sent = 0

    try:
        by_email = {c.get("email"): c for c in await xui.list_clients()}
    except Exception as e:  # noqa: BLE001
        log.error("Обход напоминаний прерван — панель недоступна: %s", e)
        return

    for user in db.all_linked_users():
        tg_id = user["tg_id"]
        client = by_email.get(user["client_email"])
        if not client:
            continue
        checked += 1

        days = xui.days_left(client)
        if days is None:  # бессрочно
            continue
        exp_ms = int(client.get("expiryTime") or 0)
        if exp_ms <= 0:
            continue
        expiry_date = dt.datetime.fromtimestamp(exp_ms / 1000).strftime("%Y-%m-%d")

        eligible = [b for b in buckets
                    if days <= b and not db.already_reminded(tg_id, expiry_date, b)]
        if not eligible:
            continue

        try:
            await bot.send_message(tg_id, texts.reminder(days))
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Напоминание не доставлено %s: %s", tg_id, e)
            continue
        # Помечаем все пороги >= days как отправленные, чтобы не слать пачкой.
        for b in buckets:
            if days <= b:
                db.mark_reminded(tg_id, expiry_date, b)

    log.info("Обход напоминаний: проверено %s, отправлено %s", checked, sent)


async def heartbeat() -> None:
    if not config.admin_ids:
        return
    msg = "💚 Бот жив, напоминания работают."
    # Суточный бэкап привязан к отбивке: так он наблюдаем — каждый день видно,
    # что копия реально ушла в R2, и сразу заметно, если перестало.
    if config.backup_enabled:
        from . import backup
        status = await backup.run_backup()
        msg += f"\n💾 Бэкап: {status}"
    try:
        await get_bot().send_message(config.admin_ids[0], msg)
    except Exception as e:  # noqa: BLE001
        log.warning("Heartbeat не отправлен: %s", e)


def setup_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="Europe/Moscow")
    sched.add_job(reminders_sweep, "cron", hour=config.remind_hour, minute=0,
                  id="reminders", misfire_grace_time=3600)
    sched.add_job(heartbeat, "cron", hour=config.remind_hour, minute=5, id="heartbeat")
    return sched
