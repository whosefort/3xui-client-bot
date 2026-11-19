from __future__ import annotations

import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ... import db
from ... import keyboards as kb
from ...config import config
from ...keyboards import settings_kb
from ..common import get_traffic_gb
from .shared import AdminFSM, router


def _settings_text_markup():
    price = db.get_setting("price", config.default_price)
    req = db.get_setting("requisites", config.default_requisites) or "(не заданы)"
    text = (f"💳 Цена: <b>{html.escape(price)} ₽</b> за {config.plan_days} дн.\n"
            f"📦 Лимит трафика: <b>{get_traffic_gb()} ГБ</b>/мес\n"
            f"🏦 Реквизиты:\n<code>{html.escape(req)}</code>")
    backup_btn = None
    if config.backup_enabled:
        paused = db.get_setting("backup_paused", "0") == "1"
        state_str = "⏸ на паузе" if paused else "✅ включён (R2, ежедневно)"
        text += f"\n💾 Бэкап: {state_str}"
        backup_btn = "▶️ Включить бэкап" if paused else "⏸ Выключить бэкап"
    else:
        text += "\n💾 Бэкап: выключен в .env (BACKUP_ENABLED=false)"
    return text, settings_kb(backup_btn)


@router.message(F.text == kb.ADM_SETTINGS)
async def kb_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = _settings_text_markup()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "bk:toggle")
async def cb_bk_toggle(cb: CallbackQuery) -> None:
    # One-click пауза/возобновление бэкапа (рантайм, без редеплоя).
    paused = db.get_setting("backup_paused", "0") == "1"
    db.set_setting("backup_paused", "0" if paused else "1")
    text, markup = _settings_text_markup()
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer("Бэкап возобновлён" if paused else "Бэкап на паузе")


@router.callback_query(F.data == "bk:now")
async def cb_bk_now(cb: CallbackQuery) -> None:
    await cb.answer("Запускаю…")
    from ... import backup
    status = await backup.run_backup()
    await cb.message.answer(f"💾 Бэкап вручную: {status}")


@router.callback_query(F.data == "set:price")
async def cb_set_price(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.set_price)
    await cb.message.answer("Введите новую цену (число), например 249:")
    await cb.answer()


@router.message(AdminFSM.set_price, F.text)
async def set_price_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    val = (message.text or "").strip()
    db.set_setting("price", val)
    await message.answer(f"✅ Цена обновлена: <b>{html.escape(val)} ₽</b>")


@router.callback_query(F.data == "set:req")
async def cb_set_req(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.set_req)
    await cb.message.answer("Введите новые реквизиты одним сообщением:")
    await cb.answer()


@router.message(AdminFSM.set_req, F.text)
async def set_req_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.set_setting("requisites", (message.text or "").strip())
    await message.answer("✅ Реквизиты обновлены.")


@router.callback_query(F.data == "set:traffic")
async def cb_set_traffic(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.set_traffic)
    await cb.message.answer(
        f"Введите месячный лимит трафика в <b>ГБ</b> (число, сейчас {get_traffic_gb()}).\n"
        f"Применится к новым выдачам и продлениям. Уже активные клиенты получат "
        f"новый лимит при следующем продлении."
    )
    await cb.answer()


@router.message(AdminFSM.set_traffic, F.text)
async def set_traffic_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    raw = (message.text or "").strip()
    if not (raw.isdigit() and 1 <= int(raw) <= 100000):
        await message.answer("Нужно целое число ГБ (1–100000). Отменено.")
        return
    db.set_setting("traffic_gb", str(int(raw)))
    await message.answer(f"✅ Месячный лимит трафика обновлён: <b>{int(raw)} ГБ</b>.")
