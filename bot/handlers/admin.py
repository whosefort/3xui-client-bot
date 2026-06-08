"""Админ-хендлеры. Доступ — только из allowlist (config.admin_ids).

Управление целиком из Telegram: постоянная клавиатура снизу + инлайн-карточки
клиентов (продлить/выключить/удалить/ссылка), список заявок, цена/реквизиты,
рассылка, выдача вручную. Юзернеймы берём из БД бота и сопоставляем с tgId
из панели (панель username не хранит — только tgId).
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb, texts
from ..config import config
from ..keyboards import (admin_kb, admin_decision, broadcast_confirm_kb,
                         broadcast_target_kb, client_card_kb, clients_list_kb,
                         confirm_delete_kb, settings_kb)
from ..runtime import get_bot, get_xui
from .common import sub_link

log = logging.getLogger("admin")
router = Router()

# Жёсткий allowlist: любые апдейты в этом роутере — только от админов.
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


class AdminFSM(StatesGroup):
    grant = State()
    broadcast_ids = State()      # ждём список получателей (для адресной рассылки)
    broadcast = State()          # ждём текст рассылки
    broadcast_confirm = State()  # текст получен, ждём подтверждения
    card_msg = State()           # ждём текст личного сообщения конкретному клиенту
    card_extend = State()        # ждём число месяцев для ручного продления
    card_bind = State()          # ждём tg_id для ручной привязки клиента
    set_price = State()
    set_req = State()


def _client_email(tg_id: int) -> str:
    return f"u{tg_id}"


# ---------- форматирование ----------

def _label(cl: dict, umap: dict[int, str]) -> str:
    """Человекочитаемое имя клиента: @username → id → email."""
    tgid = int(cl.get("tgId") or 0)
    if tgid and umap.get(tgid):
        return umap[tgid]
    if tgid:
        return f"id {tgid}"
    return cl.get("email") or "—"


def _fmt_bytes(b: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "Б" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ПБ"


def _usage(cl: dict) -> str:
    """Использованный трафик / лимит (админу показывать можно)."""
    t = cl.get("traffic") if isinstance(cl.get("traffic"), dict) else cl
    used = int(t.get("up") or 0) + int(t.get("down") or 0)
    total = int(cl.get("totalGB") or 0)
    if used == 0 and total == 0:
        return ""
    if total > 0:
        return f"{_fmt_bytes(used)} / {_fmt_bytes(total)}"
    return _fmt_bytes(used)


def _card_text(cl: dict, umap: dict[int, str]) -> str:
    xui = get_xui()
    days = xui.days_left(cl)
    enabled = cl.get("enable", True)
    if days is None:
        status = "♾ бессрочно"
    elif days <= 0 or not enabled:
        status = "⛔️ истекла / выключена"
    else:
        status = f"✅ активна, осталось <b>{days}</b> дн."

    lines = [f"👤 <b>{html.escape(_label(cl, umap))}</b>"]
    tgid = int(cl.get("tgId") or 0)
    if tgid:
        lines.append(f"tg_id: <code>{tgid}</code>")
    lines.append(f"email: <code>{html.escape(cl.get('email', ''))}</code>")
    lines.append(f"Статус: {status}")
    usage = _usage(cl)
    if usage:
        lines.append(f"Трафик: {usage}")
    return "\n".join(lines)


# =====================================================================
#  ВХОД В АДМИНКУ
# =====================================================================

@router.message(Command("start", "admin", "panel"))
async def adm_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🛠 <b>Админ-панель</b>\nВыберите действие на клавиатуре ниже.",
        reply_markup=admin_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🛠 <b>Кнопки внизу делают всё то же, но удобнее.</b>\n\n"
        "Команды (если нужно):\n"
        "/price &lt;сумма&gt; — цена тарифа\n"
        "/requisites &lt;текст&gt; — реквизиты\n"
        "/grant &lt;tg_id&gt; — выдать вручную\n"
        "/broadcast &lt;текст&gt; — рассылка\n",
        reply_markup=admin_kb(),
    )


# =====================================================================
#  ЗАЯВКИ
# =====================================================================

@router.message(F.text == kb.ADM_REQUESTS)
async def kb_requests(message: Message, state: FSMContext) -> None:
    await state.clear()
    reqs = db.pending_requests()
    if not reqs:
        await message.answer("📭 Нет заявок в обработке.")
        return
    umap = db.usernames_map()
    await message.answer(f"📋 Заявок в обработке: <b>{len(reqs)}</b>")
    for r in reqs:
        uname = r["tg_username"] or umap.get(r["tg_id"]) or f"id {r['tg_id']}"
        label = "🆕 Новая подписка" if r["type"] == "new" else "🔁 Продление"
        await message.answer(
            f"📨 Заявка #{r['id']} — <b>{label}</b>\n"
            f"От: {html.escape(str(uname))} (id <code>{r['tg_id']}</code>)\n"
            f"Тариф: {config.plan_days} дн., {db.get_setting('price', config.default_price)} ₽",
            reply_markup=admin_decision(r["id"]),
        )


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

    try:
        if req["type"] == "new":
            await _approve_new(req)
        else:
            await _approve_renew(req)
    except Exception:  # noqa: BLE001
        # Заявка НАМЕРЕННО остаётся pending — чтобы можно было повторить после
        # устранения причины (панель прилегла и т.п.). Чтобы не зависала молча,
        # явно подсказываем админу про кнопку «Отклонить» (она тоже снимает pending).
        log.exception("Ошибка подтверждения заявки %s", req_id)
        await cb.answer(
            "Не удалось подтвердить (панель недоступна?). "
            "Повторите позже или нажмите «Отклонить».",
            show_alert=True,
        )
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
        # Заявка на продление, но клиента в панели нет (старый клиент без tgId,
        # удалённый, или поддельный paid:renew). Не падаем в RuntimeError, из-за
        # которого заявка вечно висела pending — просто выдаём новую подписку.
        log.info("renew без клиента tg_id=%s → выдаём новую подписку", tg_id)
        await _approve_new(req)
        return
    res = await xui.extend_client(
        client=client, add_days=config.plan_days,
        set_total_gb=config.plan_traffic_gb, reset_traffic=True,  # свежий месяц: 150 ГБ заново
        inbound_ids=config.default_inbound_ids)
    days = xui.days_left({"expiryTime": res["expiry_ms"]})
    await _safe_user_msg(tg_id, texts.renewed(days if days is not None else config.plan_days))


# =====================================================================
#  ИСТЕКАЮЩИЕ
# =====================================================================

@router.message(F.text == kb.ADM_EXPIRING)
async def kb_expiring(message: Message, state: FSMContext) -> None:
    await state.clear()
    xui = get_xui()
    umap = db.usernames_map()
    rows = []
    for cl in await xui.list_clients():
        days = xui.days_left(cl)
        if days is not None and days <= 7:
            rows.append((days, f"• {html.escape(_label(cl, umap))} — <b>{days}</b> дн."))
    if not rows:
        await message.answer("👍 Никто не истекает в ближайшую неделю.")
        return
    rows.sort()
    await message.answer("⏳ <b>Истекают (≤7 дней):</b>\n" + "\n".join(r[1] for r in rows))


# =====================================================================
#  КЛИЕНТЫ (список + карточки)
# =====================================================================

async def _list_text_markup():
    xui = get_xui()
    umap = db.usernames_map()
    clients = await xui.list_clients()

    def sort_key(cl):
        d = xui.days_left(cl)
        return (10**9 if d is None else d, _label(cl, umap).lower())

    clients.sort(key=sort_key)
    items = []
    for cl in clients:
        days = xui.days_left(cl)
        d = "♾" if days is None else f"{days}д"
        off = "" if (days is None or days > 0) and cl.get("enable", True) else "⛔️"
        label = f"{off}{_label(cl, umap)} · {d}".strip()
        items.append((cl.get("email", ""), label))

    text = f"👥 <b>Клиенты ({len(items)})</b>\nНажмите на клиента для управления:"
    return text, clients_list_kb(items)


@router.message(F.text == kb.ADM_CLIENTS)
async def kb_clients(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _list_text_markup()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "cli:list")
async def cb_cli_list(cb: CallbackQuery) -> None:
    text, markup = await _list_text_markup()
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data == "cli:close")
async def cb_cli_close(cb: CallbackQuery) -> None:
    await cb.message.delete()
    await cb.answer()


async def _show_card(cb: CallbackQuery, email: str) -> None:
    xui = get_xui()
    cl = await xui.find_by_email(email)
    if not cl:
        await cb.answer("Клиент не найден (возможно, удалён)", show_alert=True)
        return
    umap = db.usernames_map()
    await cb.message.edit_text(
        _card_text(cl, umap),
        reply_markup=client_card_kb(email, cl.get("enable", True)),
    )


@router.callback_query(F.data.startswith("cli:open:"))
async def cb_cli_open(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    await _show_card(cb, email)
    await cb.answer()


@router.callback_query(F.data.startswith("cli:ext:"))
async def cb_cli_ext(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    xui = get_xui()
    cl = await xui.find_by_email(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await xui.extend_client(
        client=cl, add_days=config.plan_days,
        set_total_gb=config.plan_traffic_gb, reset_traffic=True,  # свежий месяц: 150 ГБ заново
        inbound_ids=config.default_inbound_ids)
    tgid = int(cl.get("tgId") or 0)
    if tgid:
        await _safe_user_msg(tgid, texts.renewed(config.plan_days))
    await _show_card(cb, email)
    await cb.answer(f"Продлено на {config.plan_days} дн. (трафик сброшен)")


@router.callback_query(F.data.startswith("cli:extn:"))
async def cb_cli_extn(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_xui().find_by_email(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.set_state(AdminFSM.card_extend)
    await state.update_data(extend_email=email)
    await cb.message.answer(
        f"📅 На сколько <b>месяцев</b> продлить? Пришли число (1–60).\n"
        f"За каждый месяц: +{config.plan_days} дн. срока и +{config.plan_traffic_gb} ГБ к лимиту "
        f"(счётчик НЕ сбрасывается). Отмена — кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.card_extend, F.text)
async def card_extend_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    email = data.get("extend_email")
    raw = (message.text or "").strip()
    if not (raw.isdigit() and 1 <= int(raw) <= 60):
        await message.answer("Нужно целое число месяцев 1–60. Отменено.")
        return
    months = int(raw)
    xui = get_xui()
    cl = await xui.find_by_email(email)
    if not cl:
        await message.answer("Клиент не найден (удалён?).")
        return
    try:
        res = await xui.extend_client(
            client=cl, add_days=months * config.plan_days,
            add_total_gb=months * config.plan_traffic_gb, reset_traffic=False,
            inbound_ids=config.default_inbound_ids)
    except Exception:  # noqa: BLE001
        log.exception("manual extend failed")
        await message.answer("❌ Ошибка панели — продлить не удалось. См. логи.")
        return
    days = xui.days_left({"expiryTime": res["expiry_ms"]})
    tgid = int(cl.get("tgId") or 0)
    if tgid > 0:
        await _safe_user_msg(tgid, texts.renewed(days if days is not None else months * config.plan_days))
    await message.answer(
        f"✅ Продлено на {months} мес. (~{days} дн. всего, +{months * config.plan_traffic_gb} ГБ к лимиту)."
    )


@router.callback_query(F.data.startswith("cli:tog:"))
async def cb_cli_tog(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    xui = get_xui()
    cl = await xui.find_by_email(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    new_state = not cl.get("enable", True)
    await xui.set_enabled(client=cl, enabled=new_state)
    tgid = int(cl.get("tgId") or 0)
    if tgid > 0:
        await _safe_user_msg(tgid, (
            "✅ Доступ к VPN возобновлён." if new_state
            else "⏸ Доступ к VPN приостановлен. По вопросам — кнопка «Связаться»."
        ))
    await _show_card(cb, email)
    await cb.answer("Включён" if new_state else "Выключен")


@router.callback_query(F.data.startswith("cli:lnk:"))
async def cb_cli_lnk(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    xui = get_xui()
    cl = await xui.find_by_email(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await cb.message.answer(
        f"🔗 Ссылка-подписка <b>{html.escape(_label(cl, db.usernames_map()))}</b>:\n"
        f"<code>{sub_link(cl.get('subId') or '')}</code>"
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cli:msg:"))
async def cb_cli_msg(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_xui().find_by_email(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    tgid = int(cl.get("tgId") or 0)
    if tgid <= 0:
        await cb.answer("У клиента не задан tgId — писать некому", show_alert=True)
        return
    await state.set_state(AdminFSM.card_msg)
    await state.update_data(msg_tgid=tgid)
    await cb.message.answer(
        f"✉️ Пришлите текст — отправлю клиенту <b>{html.escape(_label(cl, db.usernames_map()))}</b>.\n"
        f"Для отмены — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.card_msg, F.text)
async def card_msg_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    tgid = int(data.get("msg_tgid") or 0)
    text = (message.text or "").strip()
    if tgid <= 0 or not text:
        await message.answer("Отменено.")
        return
    try:
        await get_bot().send_message(tgid, html.escape(text))  # escape: см. #8
        await message.answer("✅ Отправлено.")
    except Exception as e:  # noqa: BLE001
        log.warning("Личное сообщение не доставлено %s: %s", tgid, e)
        await message.answer("⚠️ Не доставлено (клиент не запускал бота или заблокировал).")


@router.callback_query(F.data.startswith("cli:bind:"))
async def cb_cli_bind(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_xui().find_by_email(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.set_state(AdminFSM.card_bind)
    await state.update_data(bind_email=email)
    await cb.message.answer(
        f"🆔 Пришлите <b>tg_id</b>, который привязать к клиенту "
        f"<code>{html.escape(email)}</code>. Для отмены — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.card_bind, F.text)
async def card_bind_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    email = data.get("bind_email")
    tg_id = _parse_tgid(message.text or "")
    if tg_id is None:
        await message.answer("Нужен положительный числовой tg_id. Отменено.")
        return
    xui = get_xui()
    cl = await xui.find_by_email(email)
    if not cl:
        await message.answer("Клиент не найден (удалён?).")
        return
    try:
        await xui.bind_tgid(client=cl, tg_id=tg_id)
    except Exception:  # noqa: BLE001
        log.exception("manual bind failed")
        await message.answer("❌ Ошибка панели — привязать не удалось. См. логи.")
        return
    user = db.get_user(tg_id)
    db.upsert_user(tg_id, user["tg_username"] if user else None,
                   client_email=cl.get("email"), sub_id=cl.get("subId"))
    await message.answer(f"✅ Привязан tg_id <code>{tg_id}</code> к <code>{html.escape(email)}</code>.")
    await _safe_user_msg(tg_id, "🔗 Администратор привязал вашу подписку к этому чату. "
                                "Нажмите /start и «📊 Моя подписка».")


@router.callback_query(F.data.startswith("cli:del:"))
async def cb_cli_del(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    await cb.message.edit_text(
        f"🗑 Удалить клиента <code>{html.escape(email)}</code>?\n"
        f"Это отключит ему доступ безвозвратно.",
        reply_markup=confirm_delete_kb(email),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cli:delok:"))
async def cb_cli_delok(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    xui = get_xui()
    # Узнаём tgId ДО удаления, чтобы потом уведомить пользователя.
    cl = await xui.find_by_email(email)
    tgid = int(cl.get("tgId") or 0) if cl else 0
    try:
        await xui.delete_client(email)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка удаления клиента %s", email)
        await cb.answer("Не удалось удалить (панель недоступна?). См. логи.", show_alert=True)
        return
    if tgid > 0:
        await _safe_user_msg(tgid, "⛔️ Ваша подписка завершена. Для возобновления — «Купить подписку».")
    text, markup = await _list_text_markup()
    await cb.message.edit_text(f"✅ Удалён <code>{html.escape(email)}</code>.\n\n" + text,
                               reply_markup=markup)
    await cb.answer("Удалён")


# =====================================================================
#  ВЫДАТЬ ВРУЧНУЮ
# =====================================================================

@router.message(F.text == kb.ADM_GRANT)
async def kb_grant(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFSM.grant)
    await message.answer(
        "➕ Введите <b>tg_id</b> пользователя, которому выдать/продлить подписку.\n"
        "Узнать id можно у @userinfobot. Для отмены — любая кнопка снизу."
    )


def _parse_tgid(arg: str) -> int | None:
    """Только положительный tg_id. Отсекает 0 и отрицательные — иначе /grant 0
    цепляет первого непривязанного клиента, а /grant -N плодит мусор в панели."""
    arg = (arg or "").strip()
    if arg.isdigit() and int(arg) > 0:
        return int(arg)
    return None


@router.message(AdminFSM.grant, F.text)
async def grant_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg_id = _parse_tgid(message.text or "")
    if tg_id is None:
        await message.answer("Нужен положительный числовой tg_id (например 123456789). Отменено.")
        return
    await message.answer(await _do_grant(tg_id))


async def _do_grant(tg_id: int) -> str:
    xui = get_xui()
    user = db.get_user(tg_id)
    uname = user["tg_username"] if user else None
    try:
        existing = await xui.find_by_tgid(tg_id)
        if existing:
            await xui.extend_client(
                client=existing, add_days=config.plan_days,
                set_total_gb=config.plan_traffic_gb, reset_traffic=True,  # свежий месяц
                inbound_ids=config.default_inbound_ids)
            db.upsert_user(tg_id, uname, client_email=existing.get("email"),
                           sub_id=existing.get("subId"))
            await _safe_user_msg(tg_id, texts.renewed(config.plan_days))
            return f"✅ Продлено на {config.plan_days} дн. для {tg_id}"
        email = _client_email(tg_id)
        created = await xui.create_client(
            tg_id=tg_id, email=email, days=config.plan_days,
            traffic_gb=config.plan_traffic_gb, inbound_ids=config.default_inbound_ids,
        )
        db.upsert_user(tg_id, uname, client_email=email, sub_id=created["sub_id"])
        await _safe_user_msg(tg_id, texts.new_subscription_issued(
            config.plan_days, sub_link(created["sub_id"])))
        return f"✅ Создана подписка для {tg_id}"
    except Exception:  # noqa: BLE001
        log.exception("grant failed")
        return "❌ Ошибка панели — не удалось выдать/продлить. Подробности в логах."


# =====================================================================
#  ЦЕНА / РЕКВИЗИТЫ
# =====================================================================

def _settings_text_markup():
    price = db.get_setting("price", config.default_price)
    req = db.get_setting("requisites", config.default_requisites) or "(не заданы)"
    text = (f"💳 Цена: <b>{html.escape(price)} ₽</b> за {config.plan_days} дн.\n"
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
    from .. import backup
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


# =====================================================================
#  РАССЫЛКА
# =====================================================================

@router.message(F.text == kb.ADM_BROADCAST)
async def kb_broadcast(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("📢 Кому отправить рассылку?", reply_markup=broadcast_target_kb())


@router.callback_query(F.data == "bc:all")
async def cb_bc_all(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.broadcast)
    await state.update_data(recipients=None)   # None = всем привязанным
    await cb.message.edit_text(
        "📢 Рассылка <b>всем</b>. Пришлите текст одним сообщением.\n"
        "Покажу предпросмотр и спрошу подтверждение. Отмена — кнопка снизу."
    )
    await cb.answer()


@router.callback_query(F.data == "bc:some")
async def cb_bc_some(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.broadcast_ids)
    await cb.message.edit_text(
        "🎯 Пришлите получателей: <b>tg_id</b> через пробел или запятую "
        "(можно <b>@username</b> тех, кто писал боту).\nОтмена — кнопка снизу."
    )
    await cb.answer()


def _resolve_recipients(raw: str) -> tuple[list[int], list[str]]:
    """Разобрать строку в список tg_id. Поддержка @username для тех, кто писал боту."""
    umap = db.usernames_map()  # tg_id -> "@name"/"name"
    rev = {str(u).lstrip("@").lower(): tid for tid, u in umap.items()}
    ids: list[int] = []
    unknown: list[str] = []
    seen: set[int] = set()
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and int(tok) > 0:
            v = int(tok)
        else:
            v = rev.get(tok.lstrip("@").lower())
        if v and v not in seen:
            seen.add(v)
            ids.append(v)
        elif not v:
            unknown.append(tok)
    return ids, unknown


@router.message(AdminFSM.broadcast_ids, F.text)
async def broadcast_ids_input(message: Message, state: FSMContext) -> None:
    ids, unknown = _resolve_recipients(message.text or "")
    if not ids:
        await state.clear()
        await message.answer(
            "Не распознал ни одного получателя. Отменено.\n"
            "Нужны tg_id через пробел/запятую (или @username тех, кто писал боту)."
        )
        return
    await state.update_data(recipients=ids)
    await state.set_state(AdminFSM.broadcast)
    note = f"\n⚠️ Не распознаны: {html.escape(' '.join(unknown))}" if unknown else ""
    await message.answer(
        f"🎯 Получателей: <b>{len(ids)}</b>.{note}\nТеперь пришлите текст сообщения."
    )


@router.message(AdminFSM.broadcast, F.text)
async def broadcast_input(message: Message, state: FSMContext) -> None:
    # Текст НЕ рассылаем сразу — сначала предпросмотр и явное подтверждение,
    # чтобы случайное сообщение не улетело получателям.
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст — отменено.")
        await state.clear()
        return
    data = await state.get_data()
    recipients = data.get("recipients")  # None = всем
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminFSM.broadcast_confirm)
    count = len(recipients) if recipients else len(db.all_linked_users())
    whom = f"выбранным ({count})" if recipients else f"всем ({count})"
    await message.answer(
        f"📢 <b>Предпросмотр</b> — отправка {whom}:\n"
        f"━━━━━━━━━━━━━━\n{html.escape(text)}\n━━━━━━━━━━━━━━\nОтправить?",
        reply_markup=broadcast_confirm_kb(),
    )


@router.callback_query(AdminFSM.broadcast_confirm, F.data == "bc:send")
async def cb_broadcast_send(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    text = (data.get("broadcast_text") or "").strip()
    recipients = data.get("recipients")
    if not text:
        await cb.answer("Текст потерян, начните заново", show_alert=True)
        return
    await cb.message.edit_text("📢 Рассылаю…")
    sent, failed = await _do_broadcast(text, recipients)
    await cb.message.edit_text(f"📢 Готово. Доставлено: {sent}, ошибок: {failed}")
    await cb.answer()


@router.callback_query(AdminFSM.broadcast_confirm, F.data == "bc:cancel")
async def cb_broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("✖️ Рассылка отменена.")
    await cb.answer()


async def _do_broadcast(text: str, recipients: list[int] | None = None) -> tuple[int, int]:
    """Разослать текст. recipients=None → всем привязанным; иначе только указанным.
    Экранируем HTML, чтобы < > & в тексте не ломали parse_mode=HTML и доставку."""
    safe = html.escape(text)
    bot = get_bot()
    # recipients is None → всем; иначе ровно указанным (пустой список = никому,
    # а не «всем» — защита от случайной веерной отправки).
    if recipients is None:
        targets = [u["tg_id"] for u in db.all_linked_users()]
    else:
        targets = list(recipients)
    sent = failed = 0
    for tg_id in targets:
        try:
            await bot.send_message(tg_id, safe)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)  # ~20 msg/sec, в пределах лимитов Telegram
    return sent, failed


# =====================================================================
#  СЛЭШ-КОМАНДЫ (запасной путь, работают как раньше)
# =====================================================================

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
        await message.answer("Использование: /requisites Сбер 2202 ... на Имя")
        return
    db.set_setting("requisites", arg)
    await message.answer("✅ Реквизиты обновлены.")


@router.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    tg_id = _parse_tgid((message.text or "").partition(" ")[2])
    if tg_id is None:
        await message.answer("Использование: /grant 123456789 (положительный tg_id)")
        return
    await message.answer(await _do_grant(tg_id))


@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    if not config.backup_enabled:
        await message.answer("Бэкап выключен в .env (BACKUP_ENABLED=false).")
        return
    await message.answer("💾 Делаю бэкап…")
    from .. import backup
    status = await backup.run_backup()
    await message.answer(f"Бэкап: {status}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Использование: /broadcast Текст всем клиентам")
        return
    sent, failed = await _do_broadcast(text)
    await message.answer(f"📢 Рассылка завершена. Доставлено: {sent}, ошибок: {failed}")


# ---------- утилиты ----------

async def _safe_user_msg(tg_id: int, text: str) -> None:
    try:
        await get_bot().send_message(tg_id, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Не доставлено пользователю %s: %s", tg_id, e)
