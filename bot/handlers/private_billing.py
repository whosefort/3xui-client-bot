from __future__ import annotations

import sqlite3
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db, private_billing as billing
from ..config import config
from ..keyboards import ADM_PRIVATE_BILLING

router = Router(name="private_billing")
router.message.filter(F.from_user.id.in_(config.admin_ids), F.chat.type == "private")
router.callback_query.filter(F.from_user.id.in_(config.admin_ids), F.message.chat.type == "private")


class BillingInput(StatesGroup):
    plan = State()
page_size = 12


def keyboard(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text[:64], callback_data="pb:" + action)] for text, action in rows
    ])


def list_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    plans = db.private_billings()
    page = min(max(page, 0), max(0, (len(plans) - 1) // page_size))
    names = db.usernames_map()
    rows = [(f"{row['server']} · {names.get(row['tg_id'], row['tg_id'])} · {row['amount']} ₽",
             f"show:{row['id']}") for row in plans[page * page_size:(page + 1) * page_size]]
    if page:
        rows.append(("← Предыдущие", f"list:{page - 1}"))
    if (page + 1) * page_size < len(plans):
        rows.append(("Следующие →", f"list:{page + 1}"))
    rows.append(("Добавить тариф", "add"))
    return f"Частные серверы · тарифов: {len(plans)}", keyboard(rows)


def card(plan) -> tuple[str, InlineKeyboardMarkup]:
    ident, due = plan["id"], plan["due"]
    name = db.usernames_map().get(plan["tg_id"], plan["tg_id"])
    status = "Просрочен" if date.fromisoformat(due) < billing.now().date() else "Ожидает оплаты"
    reminders = bool(plan["reminders"])
    return (
        f"Частный сервер · {plan['server']}\nКлиент: {name} · ID {plan['tg_id']}\n"
        f"Тариф: {plan['amount']} ₽/мес.\nДата платежа: {billing.display_date(due)}\n"
        f"Статус: {status}\nНапоминания за 3 дня и за день: {'включены' if reminders else 'выключены'}",
        keyboard([("Отметить оплату", f"confirm:{ident}"),
                  ("Изменить тариф или дату", f"edit:{ident}"),
                  ("Выключить напоминания" if reminders else "Включить напоминания",
                   f"remind:{ident}:{int(not reminders)}"), ("← Частные серверы", "list:0")]),
    )


@router.message(Command("private_billing"))
@router.message(F.text == ADM_PRIVATE_BILLING)
async def show(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = list_page(0)
    await message.answer(text, reply_markup=markup, parse_mode=None)


@router.callback_query(F.data.startswith("pb:"))
async def action(cb: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    _, mode, *args = cb.data.split(":")
    message = cb.message
    if mode in ("add", "edit"):
        plan = db.private_billing(int(args[0])) if mode == "edit" else None
        if mode == "edit" and not plan:
            await cb.answer("Тариф не найден.", show_alert=True)
            return
        await state.clear()
        await state.set_state(BillingInput.plan)
        await state.update_data(private_billing_id=plan["id"] if plan else None)
        await cb.answer()
        if plan:
            prompt = f"Пришлите сумму и дату через |. Сейчас: {plan['amount']} | {billing.display_date(plan['due'])}."
        else:
            prompt = ("Пришлите Telegram ID или известный боту @username, название сервера, "
                      "сумму в ₽ и дату через |.\nПример: 123456789 | Частный сервер | 880 | 21.12.2026")
        await message.answer(prompt + "\nОтмена — /cancel.", parse_mode=None)
        return
    if mode == "list":
        text, markup = list_page(int(args[0]))
    else:
        ident = int(args[0])
        async with billing.lock:
            plan = db.private_billing(ident)
            if not plan:
                await cb.answer("Тариф не найден.", show_alert=True)
                return
            if mode == "remind":
                db.set_private_reminders(ident, args[1] == "1")
            elif mode == "confirm":
                await cb.answer()
                await message.edit_text(
                    f"Подтвердить получение {plan['amount']} ₽ за частный сервер {plan['server']} "
                    f"с датой платежа {billing.display_date(plan['due'])}?",
                    reply_markup=keyboard([
                        ("Подтвердить", f"paid:{ident}:{plan['due']}:{plan['amount']}"),
                        ("Отмена", f"show:{ident}"),
                    ]), parse_mode=None)
                return
            elif mode == "paid":
                _, due, amount = args
                next_due = billing.next_month(date.fromisoformat(due), plan["billing_day"])
                if not db.mark_private_paid(ident, due, int(amount), next_due.isoformat()):
                    await cb.answer("Этот платёж уже отмечен или тариф изменился.", show_alert=True)
                    return
                await cb.answer("Оплата отмечена")
                delivered = await billing.notify_payment(ident, due)
                await message.answer("Подтверждение отправлено клиенту." if delivered else
                                     "Оплата отмечена. Бот повторит отправку подтверждения.")
                text, markup = card(db.private_billing(ident))
                await message.edit_text(text, reply_markup=markup, parse_mode=None)
                return
            text, markup = card(db.private_billing(ident))
    await cb.answer()
    await message.edit_text(text, reply_markup=markup, parse_mode=None)


@router.message(BillingInput.plan, Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Изменения отменены.")


@router.message(BillingInput.plan, F.text)
async def save(message: Message, state: FSMContext) -> None:
    ident = (await state.get_data()).get("private_billing_id")
    parts = [field.strip() for field in message.text.split("|")]
    try:
        if ident is None:
            user, server, amount, due = parts
            matches = [tg_id for tg_id, name in db.usernames_map().items()
                       if user.startswith("@") and name.casefold() == user.casefold()]
            tg_id = int(user) if user.isascii() and user.isdecimal() else matches[0] if len(matches) == 1 else 0
        else:
            amount, due = parts
            current = db.private_billing(ident)
            tg_id, server = current["tg_id"], current["server"]
        amount = int(amount)
        day, month, year = map(int, due.split("."))
        due_date = date(year, month, day)
        if not (0 < tg_id < 2**52 and 0 < amount <= 1_000_000_000 and 0 < len(server) <= 64):
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Проверьте формат полей. Если бот не знает @username, укажите числовой Telegram ID.")
        return
    try:
        async with billing.lock:
            ident = db.save_private_billing(tg_id, server, amount, due_date.isoformat(), day, ident)
    except ValueError as exc:
        await message.answer(str(exc), parse_mode=None)
        return
    except sqlite3.IntegrityError:
        await message.answer("У этого клиента уже есть тариф с таким названием сервера.")
        return
    await state.clear()
    text, markup = card(db.private_billing(ident))
    await message.answer(text, reply_markup=markup, parse_mode=None)
