from __future__ import annotations

import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ... import db, texts
from ... import keyboards as kb
from ...config import config
from ...keyboards import (
    admin_decision,
    client_card_kb,
    clients_list_kb,
    confirm_delete_kb,
    confirm_unlimited_kb,
    setup_start_kb,
)
from ...panels.base import Client
from ...runtime import get_bot, get_panel
from ..common import get_traffic_gb
from .shared import AdminFSM, _safe_user_msg, log, router


def _label(cl: Client, umap: dict[int, str], labels: dict[str, str] | None = None) -> str:
    """Человекочитаемое имя клиента: ручная подпись → @username → id → username.
    Ручная подпись всегда в приоритете — админ мог назвать клиента осознанно
    иначе, чем показывает Telegram (или это клиент без tg_id вообще)."""
    if labels and labels.get(cl.username):
        return labels[cl.username]
    if cl.tg_id and umap.get(cl.tg_id):
        return umap[cl.tg_id]
    if cl.tg_id:
        return f"id {cl.tg_id}"
    return cl.username or "—"


def _fmt_bytes(b: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "Б" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ПБ"


def _usage(cl: Client) -> str:
    """Использованный трафик / лимит (админу показывать можно)."""
    used, total = cl.used_bytes, cl.limit_bytes
    if used == 0 and total == 0:
        return ""
    if total > 0:
        return f"{_fmt_bytes(used)} / {_fmt_bytes(total)}"
    return _fmt_bytes(used)


def _card_text(cl: Client, umap: dict[int, str], labels: dict[str, str] | None = None,
                note: str | None = None) -> str:
    days = cl.days_left
    if days is None:
        status = "♾ бессрочно"
    elif days <= 0 or not cl.enabled:
        status = "⛔️ истекла / выключена"
    elif cl.exhausted:
        status = "⚠️ лимит трафика исчерпан"
    else:
        status = f"✅ активна, осталось <b>{days}</b> дн."

    lines = [f"👤 <b>{html.escape(_label(cl, umap, labels))}</b>"]
    if cl.tg_id:
        lines.append(f"tg_id: <code>{cl.tg_id}</code>")
    lines.append(f"логин: <code>{html.escape(cl.username)}</code>")
    lines.append(f"Статус: {status}")
    usage = _usage(cl)
    if usage:
        lines.append(f"Трафик: {usage}")
    if note:
        lines.append(f"📝 {html.escape(note)}")
    return "\n".join(lines)


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
        if not db.claim_request(req_id):
            await cb.answer("Уже обработана кем-то другим", show_alert=True)
            return
        db.decide_request(req_id, "rejected", cb.from_user.id)
        await _safe_user_msg(req["tg_id"], "❌ Заявка отклонена. По вопросам — кнопка «Связаться».")
        await cb.message.edit_text(cb.message.html_text + "\n\n❌ <b>Отклонено</b>")
        await cb.answer("Отклонено")
        return

    # claim_request атомарно переводит pending -> processing: без этого
    # окно между проверкой status=='pending' выше и записью решения ниже
    # (внутри которого ждём панель) позволяет одобрить одну заявку дважды
    # при двойном тапе/двух админах — и клиент получает двойную выдачу.
    if not db.claim_request(req_id):
        await cb.answer("Уже обработана кем-то другим", show_alert=True)
        return

    try:
        if req["type"] == "new":
            await _approve_new(req)
        else:
            await _approve_renew(req)
    except Exception:  # noqa: BLE001
        # Заявка НАМЕРЕННО возвращается в pending — чтобы можно было повторить
        # после устранения причины (панель прилегла и т.п.). Чтобы не зависала
        # молча, явно подсказываем админу про кнопку «Отклонить» (она тоже
        # снимает processing/pending).
        log.exception("Ошибка подтверждения заявки %s", req_id)
        db.release_request(req_id)
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
    panel = get_panel()
    tg_id = req["tg_id"]
    created = await panel.create_client(
        tg_id=tg_id, days=config.plan_days, traffic_gb=get_traffic_gb())
    db.upsert_user(tg_id, req["tg_username"], client_email=created.username, sub_id=created.sub_url)
    await _safe_user_msg(tg_id, texts.new_subscription_issued(config.plan_days, created.all_subs),
                         reply_markup=setup_start_kb())


async def _approve_renew(req) -> None:
    panel = get_panel()
    tg_id = req["tg_id"]
    client = await panel.find_by_tgid(tg_id)
    if not client:
        # Заявка на продление, но клиента в панели нет (старый/удалённый/поддельный
        # renew). Не падаем в ошибку (из-за которой заявка вечно висела pending) —
        # просто выдаём новую подписку.
        log.info("renew без клиента tg_id=%s → выдаём новую подписку", tg_id)
        await _approve_new(req)
        return
    res = await panel.extend_client(
        client=client, add_days=config.plan_days,
        set_total_gb=get_traffic_gb(), reset_traffic=True)   # свежий месяц
    days = res.days_left
    await _safe_user_msg(tg_id, texts.renewed(days if days is not None else config.plan_days))


@router.message(F.text == kb.ADM_EXPIRING)
async def kb_expiring(message: Message, state: FSMContext) -> None:
    await state.clear()
    panel = get_panel()
    umap = db.usernames_map()
    rows = []
    for cl in await panel.list_clients():
        days = cl.days_left
        if days is not None and days <= 7:
            rows.append((days, f"• {html.escape(_label(cl, umap))} — <b>{days}</b> дн."))
    if not rows:
        await message.answer("👍 Никто не истекает в ближайшую неделю.")
        return
    rows.sort()
    await message.answer("⏳ <b>Истекают (≤7 дней):</b>\n" + "\n".join(r[1] for r in rows))


async def _list_text_markup():
    panel = get_panel()
    umap = db.usernames_map()
    labels = db.client_labels_map()
    clients = await panel.list_clients()

    def sort_key(cl):
        d = cl.days_left
        return (10**9 if d is None else d, _label(cl, umap, labels).lower())

    clients.sort(key=sort_key)
    items = []
    for cl in clients:
        days = cl.days_left
        d = "♾" if days is None else f"{days}д"
        off = "" if (days is None or days > 0) and cl.enabled else "⛔️"
        label = f"{off}{_label(cl, umap, labels)} · {d}".strip()
        items.append((cl.username, label))

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
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден (возможно, удалён)", show_alert=True)
        return
    umap = db.usernames_map()
    labels = db.client_labels_map()
    note = db.get_client_note(email)
    await cb.message.edit_text(
        _card_text(cl, umap, labels, note),
        reply_markup=client_card_kb(email, cl.enabled),
    )


@router.callback_query(F.data.startswith("cli:open:"))
async def cb_cli_open(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    await _show_card(cb, email)
    await cb.answer()


@router.callback_query(F.data.startswith("cli:ext:"))
async def cb_cli_ext(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    panel = get_panel()
    cl = await panel.find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await panel.extend_client(
        client=cl, add_days=config.plan_days,
        set_total_gb=get_traffic_gb(), reset_traffic=True)   # свежий месяц
    if cl.tg_id:
        await _safe_user_msg(cl.tg_id, texts.renewed(config.plan_days))
    await _show_card(cb, email)
    await cb.answer(f"Продлено на {config.plan_days} дн. (трафик сброшен)")


@router.callback_query(F.data.startswith("cli:extn:"))
async def cb_cli_extn(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.set_state(AdminFSM.card_extend)
    await state.update_data(extend_email=email)
    limit = get_traffic_gb()
    await cb.message.answer(
        f"📅 На сколько <b>месяцев</b> продлить? Пришли число (1–60).\n"
        f"Срок: +N×{config.plan_days} дн. Лимит станет <b>N×{limit} ГБ</b>, "
        f"счётчик трафика сбросится. Отмена — кнопка снизу."
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
    limit = get_traffic_gb()
    panel = get_panel()
    cl = await panel.find_by_username(email)
    if not cl:
        await message.answer("Клиент не найден (удалён?).")
        return
    try:
        # N мес: лимит = N×месячный (порог растёт при N>1), счётчик сбрасывается.
        res = await panel.extend_client(
            client=cl, add_days=months * config.plan_days,
            set_total_gb=months * limit, reset_traffic=True)
    except Exception:  # noqa: BLE001
        log.exception("manual extend failed")
        await message.answer("❌ Ошибка панели — продлить не удалось. См. логи.")
        return
    days = res.days_left
    if cl.tg_id > 0:
        await _safe_user_msg(cl.tg_id, texts.renewed(days if days is not None else months * config.plan_days))
    await message.answer(
        f"✅ Продлено на {months} мес. (~{days} дн. всего, лимит = {months * limit} ГБ, трафик сброшен)."
    )


@router.callback_query(F.data.startswith("cli:sub:"))
async def cb_cli_sub(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.set_state(AdminFSM.card_subtract)
    await state.update_data(sub_email=email)
    await cb.message.answer(
        f"➖ На сколько <b>месяцев</b> УБАВИТЬ срок? Пришли число (1–60).\n"
        f"Это коррекция даты окончания (если переборщил). Лимит и трафик не трогаю. "
        f"Отмена — кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.card_subtract, F.text)
async def card_subtract_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    email = data.get("sub_email")
    raw = (message.text or "").strip()
    if not (raw.isdigit() and 1 <= int(raw) <= 60):
        await message.answer("Нужно целое число месяцев 1–60. Отменено.")
        return
    months = int(raw)
    panel = get_panel()
    cl = await panel.find_by_username(email)
    if not cl:
        await message.answer("Клиент не найден (удалён?).")
        return
    try:
        # Убавляем срок: add_days отрицательный, лимит/счётчик не трогаем.
        res = await panel.extend_client(
            client=cl, add_days=-months * config.plan_days, reset_traffic=False)
    except Exception:  # noqa: BLE001
        log.exception("manual subtract failed")
        await message.answer("❌ Ошибка панели — изменить срок не удалось. См. логи.")
        return
    days = res.days_left
    await message.answer(f"✅ Срок уменьшен на {months} мес. Осталось ~{days} дн.")


@router.callback_query(F.data.startswith("cli:tog:"))
async def cb_cli_tog(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    panel = get_panel()
    cl = await panel.find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    new_state = not cl.enabled
    await panel.set_enabled(client=cl, enabled=new_state)
    if cl.tg_id > 0:
        await _safe_user_msg(cl.tg_id, (
            "✅ Доступ к VPN возобновлён." if new_state
            else "⏸ Доступ к VPN приостановлен. По вопросам — кнопка «Связаться»."
        ))
    await _show_card(cb, email)
    await cb.answer("Включён" if new_state else "Выключен")


@router.callback_query(F.data.startswith("cli:lnk:"))
async def cb_cli_lnk(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    links = cl.raw.get("links") or []
    links_block = ("\n\n🔗 Сырые ссылки (если у клиента таймаут на сабку — эти вставляются в клиент напрямую, без запроса к домену панели):\n"
                  + "\n".join(f"<code>{html.escape(lk)}</code>" for lk in links)) if links else ""
    await cb.message.answer(
        f"🔗 Ссылка-подписка <b>{html.escape(_label(cl, db.usernames_map()))}</b>:\n"
        + "\n".join(f"<code>{html.escape(u)}</code>" for u in cl.all_subs) + links_block
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cli:msg:"))
async def cb_cli_msg(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    tgid = cl.tg_id
    if tgid <= 0:
        await cb.answer("У клиента не задан tg_id — писать некому", show_alert=True)
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
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.set_state(AdminFSM.card_bind)
    await state.update_data(bind_email=email)
    await cb.message.answer(
        f"🆔 Пришлите <b>tg_id</b>, который привязать к клиенту "
        f"<code>{html.escape(email)}</code>.\n"
        f"Узнать id можно у @userinfobot. Для отмены — любая кнопка снизу."
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
    panel = get_panel()
    cl = await panel.find_by_username(email)
    if not cl:
        await message.answer("Клиент не найден (удалён?).")
        return
    try:
        await panel.bind_tgid(client=cl, tg_id=tg_id)
    except Exception:  # noqa: BLE001
        log.exception("manual bind failed")
        await message.answer("❌ Ошибка панели — привязать не удалось. См. логи.")
        return
    user = db.get_user(tg_id)
    db.upsert_user(tg_id, user["tg_username"] if user else None,
                   client_email=cl.username, sub_id=cl.sub_url)
    await message.answer(f"✅ Привязан tg_id <code>{tg_id}</code> к <code>{html.escape(email)}</code>.")
    await _safe_user_msg(tg_id, "🔗 Администратор привязал вашу подписку к этому чату. "
                                "Нажмите /start и «📊 Моя подписка».")


@router.callback_query(F.data.startswith("cli:label:"))
async def cb_cli_label(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.set_state(AdminFSM.card_label)
    await state.update_data(label_email=email)
    current = db.client_labels_map().get(email)
    hint = f"\nСейчас: «{html.escape(current)}»." if current else ""
    await cb.message.answer(
        f"✏️ Пришли подпись для <code>{html.escape(email)}</code> — как показывать "
        f"его в списках/карточке (например имя клиента). Пустое сообщение (пробел) "
        f"убирает подпись, возвращает автоопределение.{hint}\nОтмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.card_label, F.text)
async def card_label_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    email = data.get("label_email")
    text = (message.text or "").strip()
    if not email:
        return
    if not text:
        db.clear_client_label(email)
        await message.answer(f"✅ Подпись для <code>{html.escape(email)}</code> убрана.")
        return
    db.set_client_label(email, text, message.from_user.id)
    await message.answer(f"✅ <code>{html.escape(email)}</code> теперь подписан как «{html.escape(text)}».")


@router.callback_query(F.data.startswith("cli:note:"))
async def cb_cli_note(cb: CallbackQuery, state: FSMContext) -> None:
    email = cb.data.split(":", 2)[2]
    cl = await get_panel().find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.set_state(AdminFSM.card_note)
    await state.update_data(note_email=email)
    current = db.get_client_note(email)
    hint = f"\nСейчас: «{html.escape(current)}»." if current else ""
    await cb.message.answer(
        f"📝 Пришли описание для <code>{html.escape(email)}</code> — заметка только "
        f"для тебя, клиенту нигде не показывается. Пустое сообщение (пробел) убирает "
        f"описание.{hint}\nОтмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.card_note, F.text)
async def card_note_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    email = data.get("note_email")
    text = (message.text or "").strip()
    if not email:
        return
    if not text:
        db.clear_client_note(email)
        await message.answer(f"✅ Описание для <code>{html.escape(email)}</code> убрано.")
        return
    db.set_client_note(email, text, message.from_user.id)
    await message.answer(f"✅ Описание для <code>{html.escape(email)}</code> сохранено.")


@router.callback_query(F.data.startswith("cli:unlim:"))
async def cb_cli_unlim(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    await cb.message.edit_text(
        f"♾ Снять ВСЕ ограничения с <code>{html.escape(email)}</code>?\n"
        f"Бессрочно + безлимитный трафик, счётчик трафика сбросится. "
        f"Обычные «Продлить» после этого снова введут срок/лимит.",
        reply_markup=confirm_unlimited_kb(email),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cli:unlimok:"))
async def cb_cli_unlimok(cb: CallbackQuery) -> None:
    email = cb.data.split(":", 2)[2]
    panel = get_panel()
    cl = await panel.find_by_username(email)
    if not cl:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    try:
        await panel.set_unlimited(client=cl, reset_traffic=True)
    except Exception:  # noqa: BLE001
        log.exception("set_unlimited failed")
        await cb.answer("❌ Ошибка панели — не удалось. См. логи.", show_alert=True)
        return
    if cl.tg_id:
        await _safe_user_msg(cl.tg_id, "🎉 Ваша подписка теперь без ограничений по сроку и трафику.")
    await _show_card(cb, email)
    await cb.answer("Готово — без ограничений.")


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
    panel = get_panel()
    # Узнаём tg_id ДО удаления, чтобы потом уведомить пользователя.
    cl = await panel.find_by_username(email)
    tgid = cl.tg_id if cl else 0
    try:
        await panel.delete_client(email)
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
    panel = get_panel()
    user = db.get_user(tg_id)
    uname = user["tg_username"] if user else None
    try:
        existing = await panel.find_by_tgid(tg_id)
        if existing:
            await panel.extend_client(
                client=existing, add_days=config.plan_days,
                set_total_gb=get_traffic_gb(), reset_traffic=True)   # свежий месяц
            db.upsert_user(tg_id, uname, client_email=existing.username,
                           sub_id=existing.sub_url)
            await _safe_user_msg(tg_id, texts.renewed(config.plan_days))
            return f"✅ Продлено на {config.plan_days} дн. для {tg_id}"
        created = await panel.create_client(
            tg_id=tg_id, days=config.plan_days, traffic_gb=get_traffic_gb())
        db.upsert_user(tg_id, uname, client_email=created.username, sub_id=created.sub_url)
        await _safe_user_msg(tg_id, texts.new_subscription_issued(config.plan_days, created.all_subs),
                             reply_markup=setup_start_kb())
        return f"✅ Создана подписка для {tg_id}"
    except Exception:  # noqa: BLE001
        log.exception("grant failed")
        return "❌ Ошибка панели — не удалось выдать/продлить. Подробности в логах."
