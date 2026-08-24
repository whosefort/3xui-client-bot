"""Пользовательские хендлеры: статус, покупка/продление (заявки), поддержка."""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, texts
from ..config import config
from ..keyboards import (back_to_menu, bind_ok_kb, confirm_paid, faq_answer_kb,
                        faq_kb, main_menu, setup_done_kb, setup_step1_kb,
                        setup_step2_kb, setup_step3_kb, status_kb, sub_fallback_kb)
from ..runtime import get_panel
from .common import get_price, get_requisites, notify_admins, resolve_client

log = logging.getLogger("user")
router = Router()


class Support(StatesGroup):
    waiting = State()


class Bind(StatesGroup):
    waiting = State()


def _extract_subid(text: str) -> str:
    """Из присланной строки достать subId: принимаем как полную ссылку
    (.../<путь>/<subId>?...), так и голый id."""
    s = (text or "").strip()
    s = s.split("?")[0].split("#")[0].rstrip("/")
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s.strip()


def _uname(msg_or_cb) -> str | None:
    u = msg_or_cb.from_user
    return ("@" + u.username) if u.username else (u.full_name or None)


async def _show_menu(target: Message, tg_id: int) -> None:
    has_sub = await resolve_client(tg_id) is not None
    await target.answer("Главное меню:", reply_markup=main_menu(has_sub))


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg_id = message.from_user.id
    db.upsert_user(tg_id, _uname(message))
    has_sub = await resolve_client(tg_id) is not None
    greet = "👋 Привет! Это бот вашего VPN."
    await message.answer(greet, reply_markup=main_menu(has_sub))


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    has_sub = await resolve_client(cb.from_user.id) is not None
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu(has_sub))
    await cb.answer()


@router.callback_query(F.data == "status")
async def cb_status(cb: CallbackQuery) -> None:
    cl = await resolve_client(cb.from_user.id)
    if not cl:
        await cb.message.edit_text(texts.status_none(), reply_markup=main_menu(False))
        await cb.answer()
        return

    days = cl.days_left
    if not cl.enabled or (days is not None and days <= 0):
        text = texts.status_expired()
    elif cl.exhausted:
        # Срок ещё идёт, но трафик кончился — иначе юзер видел бы «активна»,
        # а VPN не работает (ровно та путаница, ради которой делался бот).
        text = texts.status_exhausted()
    elif days is None:
        text = texts.status_unlimited(cl.sub_url)
    else:
        text = texts.status_active(days, cl.sub_url)
    await cb.message.edit_text(text, reply_markup=status_kb())
    await cb.answer()


@router.callback_query(F.data == "faq")
async def cb_faq(cb: CallbackQuery) -> None:
    items = [(i, title) for i, title, _body in texts.FAQ_ITEMS]
    await cb.message.edit_text(texts.faq_index(), reply_markup=faq_kb(items))
    await cb.answer()


@router.callback_query(F.data.startswith("faq:"))
async def cb_faq_answer(cb: CallbackQuery) -> None:
    item_id = cb.data.split(":", 1)[1]
    body = texts.faq_answer(item_id)
    if not body:
        await cb.answer("Не нашёл такой вопрос", show_alert=True)
        return
    await cb.message.edit_text(body, reply_markup=faq_answer_kb())
    await cb.answer()


@router.callback_query(F.data == "sub_fallback")
async def cb_sub_fallback(cb: CallbackQuery) -> None:
    cl = await resolve_client(cb.from_user.id)
    if not cl:
        await cb.answer("Подписка не найдена", show_alert=True)
        return
    text = texts.sub_fallback(cl.raw.get("links"))
    await cb.message.edit_text(text, reply_markup=sub_fallback_kb())
    await cb.answer()


# ---------- мастер настройки (3 шага) ----------

@router.callback_query(F.data == "setup:1")
async def cb_setup_1(cb: CallbackQuery) -> None:
    await cb.message.edit_text(texts.setup_step1(), reply_markup=setup_step1_kb())
    await cb.answer()


@router.callback_query(F.data == "setup:2")
async def cb_setup_2(cb: CallbackQuery) -> None:
    cl = await resolve_client(cb.from_user.id)
    if not cl:
        await cb.answer("Подписка не найдена", show_alert=True)
        return
    await cb.message.edit_text(texts.setup_step2(cl.sub_url), reply_markup=setup_step2_kb())
    await cb.answer()


@router.callback_query(F.data == "setup:3")
async def cb_setup_3(cb: CallbackQuery) -> None:
    await cb.message.edit_text(texts.setup_step3(), reply_markup=setup_step3_kb())
    await cb.answer()


@router.callback_query(F.data == "setup:done")
async def cb_setup_done(cb: CallbackQuery) -> None:
    await cb.message.edit_text(texts.setup_done(), reply_markup=setup_done_kb())
    await cb.answer()


@router.callback_query(F.data.in_({"buy", "renew"}))
async def cb_buy_or_renew(cb: CallbackQuery) -> None:
    kind = cb.data
    text = texts.buy_offer(get_price(), config.plan_days, get_requisites())
    await cb.message.edit_text(text, reply_markup=confirm_paid(kind))
    await cb.answer()


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(cb: CallbackQuery) -> None:
    kind = cb.data.split(":", 1)[1]  # buy | renew
    tg_id = cb.from_user.id

    if db.has_pending_request(tg_id):
        await cb.answer("У вас уже есть заявка в обработке", show_alert=True)
        return

    req_type = "new" if kind == "buy" else "renew"
    req_id = db.create_request(tg_id, _uname(cb), req_type)

    uname = html.escape(_uname(cb) or str(tg_id))
    label = "НОВАЯ подписка" if req_type == "new" else "ПРОДЛЕНИЕ"
    from ..keyboards import admin_decision
    await notify_admins(
        f"📨 Заявка #{req_id} — <b>{label}</b>\n"
        f"От: {uname} (id <code>{tg_id}</code>)\n"
        f"Тариф: {config.plan_days} дн., {get_price()} ₽",
        reply_markup=admin_decision(req_id),
    )
    await cb.message.edit_text(texts.request_sent(), reply_markup=back_to_menu())
    await cb.answer()


# ---------- привязка существующей подписки ----------

@router.callback_query(F.data == "have_sub")
async def cb_have_sub(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Bind.waiting)
    await cb.message.edit_text(texts.bind_prompt(), reply_markup=back_to_menu())
    await cb.answer()


# Один общий лок на все привязки: без него два пользователя (или два тапа
# одного), почти одновременно назвавшие один sub_id, оба проходят проверку
# "client.tg_id ещё не занят" до того, как первый успевает реально
# привязаться — второй bind_tgid() молча перезаписывает первого владельца.
# Привязка — редкое разовое действие, глобальная сериализация тут не проблема.
_bind_lock = asyncio.Lock()


@router.message(Bind.waiting, F.text)
async def bind_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    panel = get_panel()
    me = message.from_user.id
    sub_id = _extract_subid(message.text)
    if not sub_id:
        await message.answer(texts.bind_bad_input(), reply_markup=back_to_menu())
        return
    async with _bind_lock:
        client = await panel.find_by_subid(sub_id)
        if not client:
            await message.answer(texts.bind_not_found(), reply_markup=back_to_menu())
            return
        if client.tg_id and client.tg_id != me:
            # подписка уже принадлежит другому Telegram — не даём перехватить
            await message.answer(texts.bind_taken(), reply_markup=back_to_menu())
            await notify_admins(
                f"⚠️ {html.escape(_uname(message) or str(me))} (id <code>{me}</code>) "
                f"пытался привязать чужую подписку <code>{html.escape(client.username)}</code> "
                f"(уже за tgId <code>{client.tg_id}</code>)."
            )
            return
        await panel.bind_tgid(client=client, tg_id=me)
    db.upsert_user(me, _uname(message), client_email=client.username, sub_id=client.sub_url)
    await message.answer(texts.bind_ok(), reply_markup=bind_ok_kb())
    await notify_admins(
        f"🔗 {html.escape(_uname(message) or str(me))} (id <code>{me}</code>) "
        f"привязал подписку <code>{html.escape(client.username)}</code>."
    )


# ---------- поддержка ----------

@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Support.waiting)
    await cb.message.edit_text(texts.support_prompt(), reply_markup=back_to_menu())
    await cb.answer()


@router.message(Support.waiting, F.text)
async def support_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    uname = html.escape(_uname(message) or str(message.from_user.id))
    body = html.escape(message.text)
    target = config.support_chat_id or (config.admin_ids[0] if config.admin_ids else None)
    if target:
        await message.bot.send_message(
            target,
            f"💬 Сообщение от {uname} (id <code>{message.from_user.id}</code>):\n\n{body}",
        )
    await message.answer(texts.support_forwarded())
    await _show_menu(message, message.from_user.id)
