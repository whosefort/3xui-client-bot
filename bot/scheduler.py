"""Планировщик: ежедневные напоминания об окончании подписки + heartbeat админу.

Источник правды о сроке — 3X-UI (читаем на лету). Дедуп — таблица reminders_log:
одно напоминание на (клиент, дата конца подписки, порог дней).
Даты окончания считаются по календарю Москвы.
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, private_billing, texts
from .config import config
from .keyboards import reminder_kb
from .runtime import get_bot, panel_for

log = logging.getLogger("scheduler")
_moscow = ZoneInfo("Europe/Moscow")


async def reminders_sweep(*, expiry_only: bool = False) -> None:
    bot = get_bot()
    buckets = {0}
    if not expiry_only:
        buckets.update(days for days in config.remind_days_before if days > 0)
    today = dt.datetime.now(_moscow).date()
    checked = sent = 0

    by_panel = {}

    for user in db.all_linked_users():
        # Весь разбор одного клиента — под try. Один битый клиент (мусорный
        # срок → OverflowError в fromtimestamp и т.п.) НЕ должен обрывать
        # обход: иначе все следующие юзеры молча не получат напоминание.
        try:
            tg_id = user["tg_id"]
            key = user["panel_key"]
            if key not in by_panel:
                try:
                    by_panel[key] = {c.username: c for c in await panel_for(key).list_clients()}
                except Exception as e:
                    log.warning("Панель недоступна для напоминаний (%s): %s", key or "default", type(e).__name__)
                    by_panel[key] = {}
            client = by_panel[key].get(user["client_email"])
            if not client or client.tg_id != tg_id:
                continue
            checked += 1

            exp_ts = int(client.expire_ts or 0)
            if exp_ts <= 0 or client.access_status != "active":
                continue
            expiry = dt.datetime.fromtimestamp(exp_ts, _moscow).date()
            days = (expiry - today).days
            if days < 0:
                continue
            expiry_date = expiry.isoformat()

            eligible = [b for b in buckets
                        if days <= b and not db.already_reminded(tg_id, expiry_date, b)]
            if not eligible:
                continue

            try:
                await bot.send_message(tg_id, texts.reminder(days), reply_markup=reminder_kb())
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Напоминание не доставлено %s: %s", tg_id, e)
                continue
            # Помечаем все пороги >= days как отправленные, чтобы не слать пачкой.
            for b in buckets:
                if days <= b:
                    db.mark_reminded(tg_id, expiry_date, b)
        except Exception as e:  # noqa: BLE001
            log.warning("Пропуск клиента в обходе напоминаний (tg_id=%s): %s",
                        user["tg_id"] if "tg_id" in user.keys() else "?", e)
            continue

    log.info("Обход напоминаний: проверено %s, отправлено %s", checked, sent)


async def heartbeat() -> None:
    if not config.admin_ids:
        return
    try:
        purged = db.purge_old_node_tokens()
        if purged:
            log.info("Почистил %s использованных/истёкших node_tokens", purged)
    except Exception:  # noqa: BLE001
        log.exception("Чистка node_tokens упала")
    msg = "💚 Бот жив, напоминания работают."
    # Суточный бэкап привязан к отбивке: так он наблюдаем — каждый день видно,
    # что копия реально ушла в R2, и сразу заметно, если перестало.
    # Обёрнут отдельно: сбой бэкапа НЕ должен глушить саму отбивку.
    if config.backup_enabled:
        try:
            from . import backup
            status = await backup.run_backup()
        except Exception as e:  # noqa: BLE001
            log.exception("Бэкап упал")
            status = f"⚠️ ошибка: {type(e).__name__}"
        msg += f"\n💾 Бэкап: {status}"
    try:
        await get_bot().send_message(config.admin_ids[0], msg)
    except Exception as e:  # noqa: BLE001
        log.warning("Heartbeat не отправлен: %s", e)


def setup_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="Europe/Moscow")
    sched.add_job(reminders_sweep, "cron", hour=config.remind_hour, minute=0,
                  id="reminders", misfire_grace_time=3600)
    if config.remind_hour != 0:
        sched.add_job(reminders_sweep, "cron", hour=0, minute=0, kwargs={"expiry_only": True},
                      id="expiry_day", misfire_grace_time=3600)
    # misfire_grace_time, чтобы рестарт бота около :05 не съел суточный бэкап+отбивку.
    sched.add_job(heartbeat, "cron", hour=config.remind_hour, minute=5,
                  id="heartbeat", misfire_grace_time=3600)
    sched.add_job(private_billing.sweep, "cron", minute=10, id="private_billing",
                  misfire_grace_time=3600, coalesce=True)
    return sched
