"""Админ-хендлеры. Доступ — только из allowlist (config.admin_ids).

Здесь же — подтверждение/отклонение заявок (создание и продление клиентов),
настройка цены/реквизитов, рассылка, список истекающих.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import db, texts
from ..config import config
from ..runtime import get_bot, get_xui
from .common import sub_link

log = logging.getLogger("admin")
router = Router()

# Жёсткий allowlist: любые апдейты в этом роутере — только от админов.
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


def _client_email(tg_id: int) -> str:
    return f"u{tg_id}"


# ---------- решение по заявке ----------

@router.callback_query(F.data.startswith("adm:"))
async def cb_decision(cb: CallbackQuery) -> None:
    _, action, req_id_s = cb.data.split(":")
    req_id = int(req_id_s)
    req = db.get_request(req_id)
    if not req:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if req["status"] != "pending":
        await cb.answer(f"Уже обработана: {req['status']}", show_alert=True)
        return

    if action == "no":
        db.decide_request(req_id, "rejected", cb.from_user.id)
        await _safe_user_msg(req["tg_id"], "❌ Заявка отклонена. По вопросам — кнопка «Связаться».")
        await cb.message.edit_text(cb.message.html_text + "\n\n❌ <b>Отклонено</b>")
        await cb.answer("Отклонено")
        return

    # action == ok
    try:
        if req["type"] == "new":
            await _approve_new(req)
        else:
            await _approve_renew(req)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка подтверждения заявки %s", req_id)
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return

    db.decide_request(req_id, "approved", cb.from_user.id)
    await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Подтверждено</b>")
    await cb.answer("Готово")


async def _approve_new(req) -> None:
    xui = get_xui()
    tg_id = req["tg_id"]
    email = _client_email(tg_id)
    created = await xui.create_client(
        tg_id=tg_id, email=email, days=config.plan_days,
        traffic_gb=config.plan_traffic_gb, inbound_ids=config.default_inbound_ids,
    )
    db.upsert_user(tg_id, req["tg_username"], client_email=email, sub_id=created["sub_id"])
    await _safe_user_msg(tg_id, texts.new_subscription_issued(
        config.plan_days, sub_link(created["sub_id"])))


async def _approve_renew(req) -> None:
    xui = get_xui()
    tg_id = req["tg_id"]
    client = await xui.find_by_tgid(tg_id)
    if not client:
        raise RuntimeError("Клиент с таким tgId не найден в панели")
    res = await xui.extend_client(client=client, add_days=config.plan_days)
    days = xui.days_left({"expiryTime": res["expiry_ms"]})
    await _safe_user_msg(tg_id, texts.renewed(days if days is not None else config.plan_days))


async def _safe_user_msg(tg_id: int, text: str) -> None:
    try:
        await get_bot().send_message(tg_id, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Не доставлено пользователю %s: %s", tg_id, e)


# ---------- настройки/команды ----------

@router.message(Command("admin", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🛠 <b>Админ-команды</b>\n"
        "/price &lt;сумма&gt; — цена тарифа\n"
        "/requisites &lt;текст&gt; — реквизиты для перевода\n"
        "/show — текущие цена и реквизиты\n"
        "/expiring — кто истекает в ближайшие дни\n"
        "/grant &lt;tg_id&gt; — выдать подписку вручную (без заявки)\n"
        "/broadcast &lt;текст&gt; — рассылка всем привязанным\n"
    )


@router.message(Command("price"))
async def cmd_price(message: Message) -> None:
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg:
        await message.answer("Использование: /price 249")
        return
    db.set_setting("price", arg)
    await message.answer(f"✅ Цена обновлена: <b>{html.escape(arg)} ₽</b>")


@router.message(Command("requisites"))
async def cmd_requisites(message: Message) -> None:
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg:
        await message.answer("Использование: /requisites Сбер 2202 2002 ... на Имя")
        return
    db.set_setting("requisites", arg)
    await message.answer("✅ Реквизиты обновлены.")


@router.message(Command("show"))
async def cmd_show(message: Message) -> None:
    price = db.get_setting("price", config.default_price)
    req = db.get_setting("requisites", config.default_requisites) or "(не заданы)"
    await message.answer(
        f"💳 Цена: <b>{html.escape(price)} ₽</b> за {config.plan_days} дн.\n"
        f"🏦 Реквизиты:\n<code>{html.escape(req)}</code>"
    )


@router.message(Command("expiring"))
async def cmd_expiring(message: Message) -> None:
    xui = get_xui()
    rows = []
    for cl in await xui.list_clients():
        days = xui.days_left(cl)
        if days is not None and days <= 7:
            uname = cl.get("tgId") or cl.get("email")
            rows.append((days, f"• {html.escape(str(uname))} — {days} дн."))
    if not rows:
        await message.answer("Никто не истекает в ближайшую неделю 👍")
        return
    rows.sort()
    await message.answer("⏳ <b>Истекают (≤7 дней):</b>\n" + "\n".join(r[1] for r in rows))


@router.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Использование: /grant 123456789 (tg_id пользователя)")
        return
    tg_id = int(arg)
    user = db.get_user(tg_id)
    xui = get_xui()
    try:
        existing = await xui.find_by_tgid(tg_id)
        if existing:
            await xui.extend_client(client=existing, add_days=config.plan_days)
            db.upsert_user(tg_id, user["tg_username"] if user else None,
                           client_email=existing.get("email"), sub_id=existing.get("subId"))
            await message.answer(f"✅ Продлено на {config.plan_days} дн. для {tg_id}")
        else:
            email = _client_email(tg_id)
            created = await xui.create_client(
                tg_id=tg_id, email=email, days=config.plan_days,
                traffic_gb=config.plan_traffic_gb, inbound_ids=config.default_inbound_ids,
            )
            db.upsert_user(tg_id, user["tg_username"] if user else None,
                           client_email=email, sub_id=created["sub_id"])
            await message.answer(f"✅ Создана подписка для {tg_id}")
        await _safe_user_msg(tg_id, "🎉 Администратор выдал вам подписку. Нажмите /start.")
    except Exception as e:  # noqa: BLE001
        log.exception("grant failed")
        await message.answer(f"Ошибка: {e}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Использование: /broadcast Текст сообщения всем клиентам")
        return
    bot = get_bot()
    sent = failed = 0
    for user in db.all_linked_users():
        try:
            await bot.send_message(user["tg_id"], text)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await _throttle()
    await message.answer(f"📢 Рассылка завершена. Доставлено: {sent}, ошибок: {failed}")


async def _throttle() -> None:
    import asyncio
    await asyncio.sleep(0.05)  # ~20 msg/sec, в пределах лимитов Telegram
