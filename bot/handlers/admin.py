"""Админ-хендлеры. Доступ — только из allowlist (config.admin_ids).

Управление целиком из Telegram: постоянная клавиатура снизу + инлайн-карточки
клиентов (продлить/выключить/удалить/ссылка), список заявок, цена/реквизиты,
рассылка, выдача вручную. Юзернеймы берём из БД бота и сопоставляем с tgId
из панели (панель username не хранит — только tgId).
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import subprocess

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb, node_provision, reality_admin, reality_scan, ssh_ops, texts, warp, warp_admin
from ..config import config
from ..keyboards import (admin_kb, admin_decision, broadcast_confirm_kb,
                         broadcast_target_kb, client_card_kb, clients_list_kb,
                         confirm_delete_kb, confirm_unlimited_kb, reality_confirm_kb,
                         reality_fp_picker_kb, reality_menu_kb, reality_port_picker_kb,
                         reality_scan_results_kb, reality_sids_kb, reality_spx_picker_kb,
                         reality_sni_result_kb,
                         server_card_kb, server_fp_picker_kb, server_sni_picker_kb,
                         settings_kb, setup_start_kb, warp_domadd_categories_kb,
                         warp_domadd_domains_kb, warp_node_kb, warp_nodes_kb,
                         xray_channel_pick_kb, xray_upgrade_confirm_kb)
from ..panels.base import Client
from ..runtime import get_bot, get_panel
from .common import get_traffic_gb

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
    card_subtract = State()      # ждём число месяцев для коррекции (убавить срок)
    card_bind = State()          # ждём tg_id для ручной привязки клиента
    set_price = State()
    set_req = State()
    set_traffic = State()        # ждём новый месячный лимит трафика (ГБ)
    add_server = State()         # ждём "IP [имя]" новой ноды
    card_label = State()         # ждём текст ручной подписи клиента
    card_note = State()          # ждём текст описания клиента (для админа)
    rename_server = State()      # ждём новое имя сервера (remark хоста)
    server_sni = State()         # ждём домен для персонального SNI одной ноды
    server_sni_scan = State()    # ждём цель скана для персонального SNI одной ноды
    reality_sni = State()        # ждём домен для смены SNI
    reality_scan = State()       # ждём цель (IP/CIDR/домен) для скана RealiTLScanner
    reality_port = State()       # ждём новый порт REALITY-инбаунда
    reality_spx = State()        # ждём новый SpiderX-путь
    warp_domain = State()        # ждём домен для ручной маршрутизации через WARP


# ---------- форматирование ----------

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


# =====================================================================
#  ВХОД В АДМИНКУ
# =====================================================================

def _add_server_available() -> bool:
    return config.panel_backend == "marzban" and config.node_provision_enabled


@router.message(Command("start", "admin", "panel"))
async def adm_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🛠 <b>Админ-панель</b>\nВыберите действие на клавиатуре ниже.",
        reply_markup=admin_kb(_add_server_available(), _servers_available(), _servers_available()),
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
        reply_markup=admin_kb(_add_server_available(), _servers_available(), _servers_available()),
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
    await _safe_user_msg(tg_id, texts.new_subscription_issued(config.plan_days, created.sub_url),
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


# =====================================================================
#  ИСТЕКАЮЩИЕ
# =====================================================================

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


# =====================================================================
#  КЛИЕНТЫ (список + карточки)
# =====================================================================

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
        f"<code>{cl.sub_url}</code>{links_block}"
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
        await _safe_user_msg(tg_id, texts.new_subscription_issued(config.plan_days, created.sub_url),
                             reply_markup=setup_start_kb())
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


# =====================================================================
#  ДОБАВИТЬ СЕРВЕР (авторазвёртывание ноды)
# =====================================================================

@router.message(F.text == kb.ADM_ADD_SERVER)
async def kb_add_server(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not _add_server_available():
        await message.answer(
            "Недоступно: нужен PANEL_BACKEND=marzban и NODE_PROVISION_ENABLED=true в .env."
        )
        return
    await state.set_state(AdminFSM.add_server)
    await message.answer(
        "🖥 Пришли <b>публичный IP</b> новой ноды. Можно через пробел добавить имя:\n"
        "<code>203.0.113.10 eu-frankfurt-02</code>\n\n"
        "По умолчанию образ marzban-node — зафиксированная стабильная версия, "
        "xray-core — пин из node/XRAY_VERSION. Ключевые слова (в любом месте, "
        "через пробел), чтобы взять свежее:\n"
        "• <code>latest</code> — образ marzban-node :latest\n"
        "• <code>xraylatest</code> — xray-core последний тег с GitHub (xray-core "
        "выходит часто, пин может отставать)\n"
        "<code>203.0.113.10 eu-frankfurt-02 xraylatest</code>\n\n"
        "VPS должен быть чистым (Ubuntu/Debian), root-доступ понадобится тебе — "
        "не мне и не боту, я его не спрашиваю."
    )


@router.message(AdminFSM.add_server, F.text)
async def add_server_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    tokens = (message.text or "").strip().split()
    if not tokens:
        await message.answer("Пусто. Отменено.")
        return
    address = tokens[0]
    rest = tokens[1:]
    image_channel = "stable"
    xray_channel = "stable"
    filtered = []
    for t in rest:
        low = t.lower()
        if low == "latest":
            image_channel = "latest"
        elif low == "xraylatest":
            xray_channel = "latest"
        else:
            filtered.append(t)
    rest = filtered
    name = " ".join(rest) if rest else address
    try:
        ipaddress.ip_address(address)
    except ValueError:
        await message.answer(f"«{address}» не похож на IP-адрес. Отменено.")
        return

    await message.answer("Регистрирую ноду в панели…")
    try:
        reg = await node_provision.register_node(name, address)
    except node_provision.ProvisionError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("add_server: register_node failed")
        await message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return

    token = node_provision.new_token()
    db.create_node_token(
        token=token, node_id=reg["node_id"], node_name=name, address=address,
        cert_pem=reg["cert_pem"], panel_ip=reg["panel_ip"],
        ttl_seconds=config.node_token_ttl_seconds, created_by=message.from_user.id,
        xray_channel=xray_channel,
    )

    claim_url = f"{config.marzban_url}:{config.node_provision_port}/nodeprovision/claim"
    ttl_min = config.node_token_ttl_seconds // 60
    channel_env = " NODE_IMAGE_CHANNEL=latest" if image_channel == "latest" else ""
    cmd = (
        f"curl -fsSL https://raw.githubusercontent.com/whosefort/3xui-client-bot/main/"
        f"node/bootstrap_token.sh | NODE_TOKEN={token} CLAIM_URL={claim_url}{channel_env} bash"
    )
    image_note = "образ marzban-node: :latest (выбрано вручную)" if image_channel == "latest" \
        else "образ marzban-node: зафиксированная стабильная версия"
    xray_note = "xray-core: последний тег с GitHub (выбрано вручную)" if xray_channel == "latest" \
        else "xray-core: пин из node/XRAY_VERSION"
    await message.answer(
        f"✅ Нода id={reg['node_id']} зарегистрирована в панели ({address}).\n"
        f"Токен живёт {ttl_min} мин, одноразовый. {image_note}, {xray_note}.\n\n"
        f"Вставь эту команду в SSH-сессию нового VPS (под root):\n\n"
        f"<code>{html.escape(cmd)}</code>"
    )


# =====================================================================
#  СЕРВЕРЫ (переименование того, что видит клиент в приложении)
# =====================================================================

def _servers_available() -> bool:
    return config.panel_backend == "marzban"


def _server_label(s: dict, status_map: dict[str, str] | None = None) -> str:
    name = s["remark"] or s["address"] or f"{s['tag']}[{s['index']}]"
    if status_map is None:
        return name
    st = status_map.get(s["address"])
    icon = "🟢 " if st == "connected" else "🔴 " if st else "⚪️ "
    return icon + name


async def _node_status_map() -> dict[str, str]:
    """address -> статус из /api/nodes. Пусто при ошибке — тогда список
    серверов просто покажется без иконок, не роняем весь экран из-за этого."""
    try:
        nodes = await node_provision.list_nodes()
    except node_provision.ProvisionError:
        return {}
    return {n["address"]: n.get("status", "") for n in nodes}


@router.message(F.text == kb.ADM_SERVERS)
async def kb_servers(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not _servers_available():
        await message.answer("Недоступно: нужен PANEL_BACKEND=marzban.")
        return
    try:
        servers = await node_provision.list_servers()
    except node_provision.ProvisionError as e:
        await message.answer(f"❌ Не удалось получить список: {e}")
        return
    if not servers:
        await message.answer("Серверов пока нет.")
        return
    status_map = await _node_status_map()
    items = [(f"{s['tag']}:{s['index']}", _server_label(s, status_map)) for s in servers]
    await message.answer(
        f"🌍 Серверов: <b>{len(servers)}</b>\n"
        f"Имя в списке — то, что клиент видит в своём VPN-приложении. "
        f"🟢/🔴 — статус ноды в панели (connected/нет).",
        reply_markup=kb.servers_list_kb(items),
    )


@router.callback_query(F.data == "srv:close")
async def cb_srv_close(cb: CallbackQuery) -> None:
    await cb.message.delete()
    await cb.answer()


@router.callback_query(F.data == "srv:list")
async def cb_srv_list(cb: CallbackQuery) -> None:
    try:
        servers = await node_provision.list_servers()
    except node_provision.ProvisionError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    status_map = await _node_status_map()
    items = [(f"{s['tag']}:{s['index']}", _server_label(s, status_map)) for s in servers]
    await cb.message.edit_text(
        f"🌍 Серверов: <b>{len(servers)}</b>\n"
        f"Имя в списке — то, что клиент видит в своём VPN-приложении. "
        f"🟢/🔴 — статус ноды в панели (connected/нет).",
        reply_markup=kb.servers_list_kb(items),
    )
    await cb.answer()


def _server_card_text(match: dict, tag: str) -> str:
    sni_line = f"SNI: <code>{html.escape(match['sni'])}</code> (свой)" if match["sni"] \
        else "SNI: общий дефолт кластера"
    frag_line = "Fragment: включён (обход DPI по сигнатуре ClientHello)" if match["fragment"] \
        else "Fragment: выключен"
    fp_line = f"TLS-фингерпринт: <code>{html.escape(match['fp'])}</code>"
    return (
        f"🖥 <b>{html.escape(_server_label(match))}</b>\n"
        f"Адрес: <code>{html.escape(match['address'])}</code>\n"
        f"Инбаунд: <code>{html.escape(tag)}</code>\n"
        f"{sni_line}\n"
        f"{frag_line}\n"
        f"{fp_line}"
    )


async def _find_server(tag: str, idx: str) -> dict | None:
    servers = await node_provision.list_servers()
    return next((s for s in servers if s["tag"] == tag and str(s["index"]) == idx), None)


@router.callback_query(F.data.startswith("srv:open:"))
async def cb_srv_open(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    tag, _, idx = key.partition(":")
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    if not match:
        await cb.answer("Сервер не найден (список изменился)", show_alert=True)
        return
    await cb.message.edit_text(
        _server_card_text(match, tag),
        reply_markup=server_card_kb(key, fragment_on=match["fragment"]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv:rename:"))
async def cb_srv_rename(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":", 2)[2]
    await state.set_state(AdminFSM.rename_server)
    await state.update_data(rename_key=key)
    await cb.message.answer(
        "✏️ Пришли новое имя сервера — так его увидит клиент в приложении, "
        "например <code>Germany-1</code>. Без технических деталей.\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.rename_server, F.text)
async def rename_server_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    key = data.get("rename_key")
    text = (message.text or "").strip()
    if not key or not text:
        await message.answer("Пусто. Отменено.")
        return
    tag, _, idx = key.partition(":")
    try:
        await node_provision.rename_server(tag, int(idx), text)
    except node_provision.ProvisionError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("rename_server_input: rename_server failed")
        await message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await message.answer(f"✅ Сервер теперь называется «{html.escape(text)}».")


@router.callback_query(F.data.startswith("srv:sni:"))
async def cb_srv_sni(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":", 2)[2]
    await state.set_state(AdminFSM.server_sni)
    await state.update_data(sni_key=key)

    try:
        settings = await reality_admin.get_settings()
        used = list(settings["server_names"])
    except reality_admin.RealityError:
        used = []
    suggestions = used + [d for d in reality_admin.SUGGESTED_DOMAINS if d not in used]

    await cb.message.answer(
        "🎭 Домен-камуфляж только для ЭТОЙ ноды — выбери из предложенных или "
        "впиши свой. Проверю TLS1.3/серт перед применением.\n\n"
        "⚠️ Это не полная изоляция: ядро xray у всех нод общее и технически "
        "примет и другие SNI из общего списка — но клиенты именно этой ноды "
        "в ссылках увидят только выбранный домен.",
        reply_markup=server_sni_picker_kb(suggestions[:8]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv:snimanual")
async def cb_srv_snimanual(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("sni_key"):
        await cb.answer("Сессия истекла, начни заново из карточки сервера", show_alert=True)
        return
    await cb.message.answer(
        "Пришли домен текстом (например <code>www.speedtest.net</code>).\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv:snipick:"))
async def cb_srv_snipick(cb: CallbackQuery, state: FSMContext) -> None:
    domain = cb.data.split(":", 2)[2]
    await cb.answer()
    await _apply_host_sni(cb.message, state, domain)


@router.callback_query(F.data == "srv:sniscan")
async def cb_srv_sniscan(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("sni_key"):
        await cb.answer("Сессия истекла, начни заново из карточки сервера", show_alert=True)
        return
    await state.set_state(AdminFSM.server_sni_scan)
    await cb.message.answer(
        "🔍 Пришли цель для скана — IP, домен или подсеть не крупнее /24 "
        "(например <code>1.2.3.0/24</code>). Может занять до пары минут.\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.server_sni_scan, F.text)
async def server_sni_scan_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    sni_key = data.get("sni_key")
    if not sni_key:
        await state.clear()
        await message.answer("Сессия истекла. Начни заново из карточки сервера.")
        return
    target = (message.text or "").strip()
    if not target:
        await message.answer("Пусто. Отменено.")
        return
    await message.answer(f"Сканирую {html.escape(target)}…")
    try:
        results = await reality_scan.scan(target)
    except reality_scan.ScanError as e:
        await message.answer(f"❌ {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("server_sni_scan_input: reality_scan.scan failed")
        await message.answer("❌ Ошибка при сканировании. См. логи.")
        return
    if not results:
        await message.answer("Ничего подходящего не нашёл в этой цели.")
        return
    # Возвращаемся в server_sni: следующий тап (пик из результатов или снова
    # свой домен) должен применяться именно к этой ноде, sni_key уже в data.
    await state.set_state(AdminFSM.server_sni)
    domains = [r["domain"] for r in results]
    await message.answer(
        "🔍 Нашёл кандидатов — выбери, чтобы проверить и применить к этой ноде:",
        reply_markup=server_sni_picker_kb(domains, show_scan=False),
    )


@router.message(AdminFSM.server_sni, F.text)
async def server_sni_input(message: Message, state: FSMContext) -> None:
    domain = (message.text or "").strip().lower()
    await _apply_host_sni(message, state, domain)


async def _apply_host_sni(message: Message, state: FSMContext, domain: str) -> None:
    data = await state.get_data()
    key = data.get("sni_key")
    if not key or not domain or " " in domain or "/" in domain:
        await state.clear()
        await message.answer("Не похоже на домен. Отменено.")
        return
    await message.answer(f"Проверяю {html.escape(domain)}…")
    result = await reality_admin.check_sni_candidate(domain)
    if not result["ok"]:
        await state.clear()
        await message.answer(
            f"❌ <code>{html.escape(domain)}</code> не прошёл проверку: "
            f"{html.escape(result['reason'])}. Отменено — попробуй другой домен."
        )
        return
    await state.clear()
    tag, _, idx = key.partition(":")
    try:
        await reality_admin.set_host_sni(tag, int(idx), domain)
    except reality_admin.RealityError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("_apply_host_sni: set_host_sni failed")
        await message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await message.answer(f"✅ Эта нода теперь светит SNI <code>{html.escape(domain)}</code>.")


@router.callback_query(F.data.startswith("srv:snireset:"))
async def cb_srv_snireset(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    tag, _, idx = key.partition(":")
    try:
        await reality_admin.set_host_sni(tag, int(idx), None)
    except reality_admin.RealityError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_srv_snireset failed")
        await cb.answer("Ошибка при обращении к панели", show_alert=True)
        return
    await cb.answer("SNI сброшен на общий дефолт")
    await cb.message.answer("♻️ SNI этой ноды сброшен на общий дефолт кластера.")


@router.callback_query(F.data.startswith("srv:fragtoggle:"))
async def cb_srv_fragtoggle(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    tag, _, idx = key.partition(":")
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    if not match:
        await cb.answer("Сервер не найден (список изменился)", show_alert=True)
        return
    new_state = not match["fragment"]
    try:
        await reality_admin.set_host_fragment(tag, int(idx), new_state)
    except reality_admin.RealityError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_srv_fragtoggle failed")
        await cb.answer("Ошибка при обращении к панели", show_alert=True)
        return
    await cb.answer("Fragment включён" if new_state else "Fragment выключен")
    match["fragment"] = new_state
    await cb.message.edit_text(
        _server_card_text(match, tag),
        reply_markup=server_card_kb(key, fragment_on=new_state),
    )


@router.callback_query(F.data.startswith("srv:fp:"))
async def cb_srv_fp(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    tag, _, idx = key.partition(":")
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    if not match:
        await cb.answer("Сервер не найден (список изменился)", show_alert=True)
        return
    await cb.message.edit_text(
        f"🫆 TLS-фингерпринт для <b>{html.escape(_server_label(match))}</b>. "
        f"«randomized» — xray сам берёт случайный на каждое соединение, надёжнее "
        f"фиксированного значения на всю ноду.",
        reply_markup=server_fp_picker_kb(key, reality_admin.FINGERPRINT_OPTIONS, match["fp"]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv:fppick:"))
async def cb_srv_fppick(cb: CallbackQuery) -> None:
    rest = cb.data[len("srv:fppick:"):]
    fp, _, key = rest.partition(":")
    tag, _, idx = key.partition(":")
    try:
        await reality_admin.set_host_fingerprint(tag, int(idx), fp)
    except reality_admin.RealityError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_srv_fppick failed")
        await cb.answer("Ошибка при обращении к панели", show_alert=True)
        return
    await cb.answer(f"Fingerprint: {fp}")
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError:
        return
    if not match:
        return
    await cb.message.edit_text(
        _server_card_text(match, tag),
        reply_markup=server_card_kb(key, fragment_on=match["fragment"]),
    )


def _xray_pin() -> str:
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "node", "XRAY_VERSION")
    with open(os.path.normpath(path)) as f:
        return f.read().strip()


async def _github_latest_xray_version() -> str | None:
    """Живой запрос к GitHub API — не полагаемся на node/XRAY_VERSION, тот
    пин обновляется руками и xray-core выходит часто (см. node/DISASTER_RECOVERY.md
    контекст в чате: залипание на старом пине уже путали с сетевыми проблемами)."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=5",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json(content_type=None)
                if r.status >= 400 or not data:
                    return None
                return data[0].get("tag_name")
    except Exception:  # noqa: BLE001
        log.exception("не смог получить latest xray-core с GitHub")
        return None


@router.callback_query(F.data == "srv:xrayup")
async def cb_srv_xrayup(cb: CallbackQuery) -> None:
    try:
        pin = _xray_pin()
    except OSError:
        await cb.answer("node/XRAY_VERSION не найден", show_alert=True)
        return
    await cb.message.answer(
        "🔄 Обновить xray-core на ВСЕХ подключённых нодах по SSH? Ядро на "
        "каждой ноде перезапустится — клиенты на ней разорвут соединение на "
        "пару секунд.\n\n"
        "Стабильная — уже проверенный пин из node/XRAY_VERSION. Последняя — "
        "живой запрос к GitHub, xray-core обновляется часто, пин мог отстать.",
        reply_markup=xray_channel_pick_kb(pin),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv:xrayver:"))
async def cb_srv_xrayver(cb: CallbackQuery) -> None:
    channel = cb.data.split(":", 2)[2]
    if channel == "stable":
        try:
            version = _xray_pin()
        except OSError:
            await cb.answer("node/XRAY_VERSION не найден", show_alert=True)
            return
    else:
        await cb.answer("Спрашиваю GitHub…")
        version = await _github_latest_xray_version()
        if not version:
            await cb.message.answer("❌ Не смог получить последнюю версию с GitHub. Попробуй ещё раз позже.")
            return
    await cb.message.edit_text(
        f"Обновить xray-core до <code>{html.escape(version)}</code> на ВСЕХ нодах?",
        reply_markup=xray_upgrade_confirm_kb(version),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv:xrayupgo:"))
async def cb_srv_xrayupgo(cb: CallbackQuery) -> None:
    await cb.answer()
    pin = cb.data.split(":", 2)[2]
    try:
        nodes = await node_provision.list_nodes()
    except node_provision.ProvisionError as e:
        await cb.message.answer(f"❌ Не смог получить список нод: {e}")
        return
    if not nodes:
        await cb.message.answer("Нод нет.")
        return

    await cb.message.answer(f"Обновляю {len(nodes)} нод(ы) до {html.escape(pin)}…")
    lines = []
    for n in nodes:
        name, address = n.get("name", "?"), n.get("address", "")
        try:
            await ssh_ops.upgrade_node(address, pin)
            lines.append(f"✅ {html.escape(name)} ({html.escape(address)})")
        except ssh_ops.SSHOpError as e:
            lines.append(f"❌ {html.escape(name)} ({html.escape(address)}): {html.escape(str(e))}")
        except Exception:  # noqa: BLE001
            log.exception("xray upgrade failed for node %s", name)
            lines.append(f"❌ {html.escape(name)} ({html.escape(address)}): см. логи бота")
    await cb.message.answer("Готово:\n" + "\n".join(lines))


# ---------- честная проверка туннеля ----------

async def _verify_one(address: str, name: str) -> str:
    """Один прогон honest-check: тестовый юзер в панели → xray-клиент прямо
    на ноде → curl через него. Юзер чистится ВСЕГДА, даже если сам тест упал
    на середине — иначе панель обрастает мусорными verify-* аккаунтами."""
    try:
        client = await node_provision.create_verify_client()
    except node_provision.ProvisionError as e:
        return f"❌ {html.escape(name)}: не смог завести тестового юзера — {html.escape(str(e))}"
    if client is None:
        return f"⚠️ {html.escape(name)}: в панели ещё нет REALITY-инбаунда, нечего проверять"
    try:
        await ssh_ops.verify_reality_tunnel(address, client)
        return f"✅ {html.escape(name)} ({html.escape(address)}): туннель работает"
    except ssh_ops.SSHOpError as e:
        return f"❌ {html.escape(name)} ({html.escape(address)}): {html.escape(str(e))}"
    except Exception:  # noqa: BLE001
        log.exception("verify_reality_tunnel failed for %s", address)
        return f"❌ {html.escape(name)} ({html.escape(address)}): см. логи бота"
    finally:
        try:
            await node_provision.delete_verify_client(client["username"])
        except node_provision.ProvisionError:
            log.exception("не смог удалить тестового юзера %s после проверки", client["username"])


@router.callback_query(F.data.startswith("srv:verify:"))
async def cb_srv_verify(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    tag, _, idx = key.partition(":")
    await cb.answer()
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError as e:
        await cb.message.answer(f"❌ Ошибка: {e}")
        return
    if not match:
        await cb.message.answer("Сервер не найден (список изменился).")
        return
    await cb.message.answer(f"🩺 Проверяю {html.escape(_server_label(match))}…")
    result = await _verify_one(match["address"], _server_label(match))
    await cb.message.answer(result)


@router.callback_query(F.data == "srv:verifyall")
async def cb_srv_verifyall(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        nodes = await node_provision.list_nodes()
    except node_provision.ProvisionError as e:
        await cb.message.answer(f"❌ Не смог получить список нод: {e}")
        return
    if not nodes:
        await cb.message.answer("Нод нет.")
        return
    await cb.message.answer(f"🩺 Проверяю {len(nodes)} нод(ы) по очереди…")
    lines = [await _verify_one(n["address"], n.get("name", "?")) for n in nodes]
    await cb.message.answer("Готово:\n" + "\n".join(lines))


# ---------- ресурсы сервера (RAM/диск) ----------

@router.callback_query(F.data.startswith("srv:res:"))
async def cb_srv_res(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    tag, _, idx = key.partition(":")
    await cb.answer()
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError as e:
        await cb.message.answer(f"❌ Ошибка: {e}")
        return
    if not match:
        await cb.message.answer("Сервер не найден (список изменился).")
        return
    try:
        out = await ssh_ops.get_resources(match["address"])
    except ssh_ops.SSHOpError as e:
        await cb.message.answer(f"❌ {e}")
        return
    await cb.message.answer(
        f"📊 <b>{html.escape(_server_label(match))}</b>\n<pre>{html.escape(out)}</pre>"
    )


@router.callback_query(F.data == "srv:resources")
async def cb_srv_resources(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        nodes = await node_provision.list_nodes()
    except node_provision.ProvisionError as e:
        await cb.message.answer(f"❌ Не смог получить список нод: {e}")
        return
    parts = []
    try:
        # df на "/" внутри контейнера показал бы overlay-файловую систему
        # САМОГО контейнера, а не диск хоста. /app/data — bind-mount
        # (см. docker-compose.yml), df на нём корректно отдаёт диск хоста.
        proc = await asyncio.to_thread(
            subprocess.run, ["sh", "-c", "free -h; echo '---DISK (хост)---'; df -h /app/data"],
            capture_output=True, text=True, timeout=10,
        )
        parts.append(f"📊 <b>Мастер (этот сервер)</b>\n<pre>{html.escape(proc.stdout.strip())}</pre>")
    except Exception:  # noqa: BLE001
        log.exception("resources check on master failed")
        parts.append("📊 <b>Мастер</b>: не смог проверить, см. логи")
    for n in nodes:
        try:
            out = await ssh_ops.get_resources(n["address"])
            parts.append(f"📊 <b>{html.escape(n.get('name', '?'))}</b>\n<pre>{html.escape(out)}</pre>")
        except ssh_ops.SSHOpError as e:
            parts.append(f"📊 <b>{html.escape(n.get('name', '?'))}</b>: {html.escape(str(e))}")
    for part in parts:
        await cb.message.answer(part)


# =====================================================================
#  СОЕДИНЕНИЕ (SNI-камуфляж, ключи, shortId REALITY)
# =====================================================================

async def _reality_menu_text() -> tuple[str, bool]:
    try:
        s = await reality_admin.get_settings()
        ru_block_on = await reality_admin.get_ru_block_enabled()
    except reality_admin.RealityError as e:
        return f"❌ Не удалось получить настройки: {e}", False
    sni = ", ".join(s["server_names"]) or "—"
    spx_line = f"SpiderX: <code>{html.escape(s['spx'])}</code>" if s["spx"] else "SpiderX: не задан"
    ru_line = "Блок .ru/.su на ноде: включён" if ru_block_on else "Блок .ru/.su на ноде: выключен"
    text = (
        f"🛡 <b>Соединение (REALITY)</b>\n"
        f"Камуфляж (SNI): <code>{html.escape(sni)}</code>\n"
        f"dest: <code>{html.escape(s['dest'])}</code>\n"
        f"Порт: <b>{s['port']}</b>\n"
        f"publicKey: <code>{html.escape(s['public_key'])}</code>\n"
        f"shortId: <b>{len(s['short_ids'])}</b> шт.\n"
        f"{spx_line}\n"
        f"{ru_line}\n\n"
        f"Правки применяются сразу на весь кластер (общий конфиг), без "
        f"грейс-периода — старые ключи/shortId перестают работать немедленно. "
        f"Подписка генерируется на лету, отдельно рассылать новые ссылки не надо."
    )
    return text, ru_block_on


@router.message(F.text == kb.ADM_REALITY)
async def kb_reality(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not _servers_available():
        await message.answer("Недоступно: нужен PANEL_BACKEND=marzban.")
        return
    text, ru_block_on = await _reality_menu_text()
    await message.answer(text, reply_markup=reality_menu_kb(ru_block_on))


@router.callback_query(F.data == "rl:close")
async def cb_rl_close(cb: CallbackQuery) -> None:
    await cb.message.delete()
    await cb.answer()


@router.callback_query(F.data == "rl:menu")
async def cb_rl_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, ru_block_on = await _reality_menu_text()
    await cb.message.edit_text(text, reply_markup=reality_menu_kb(ru_block_on))
    await cb.answer()


@router.callback_query(F.data == "rl:rublocktoggle")
async def cb_rl_rublocktoggle(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        current = await reality_admin.get_ru_block_enabled()
        await reality_admin.set_ru_block(not current)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_rublocktoggle failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    text, ru_block_on = await _reality_menu_text()
    await cb.message.edit_text(text, reply_markup=reality_menu_kb(ru_block_on))
    await cb.answer()


@router.callback_query(F.data == "rl:sni")
async def cb_rl_sni(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer("🔄 Проверяю варианты…")
    validated = await reality_admin.validate_candidates(reality_admin.SUGGESTED_DOMAINS)
    if not validated:
        await cb.message.answer(
            "Ни один из обычных вариантов сейчас не прошёл проверку — "
            "введи домен вручную или запусти скан.",
            reply_markup=reality_scan_results_kb([], show_scan=True),
        )
        return
    await cb.message.answer(
        "🔄 Прошли проверку прямо сейчас — выбери новый SNI-камуфляж:",
        reply_markup=reality_scan_results_kb(validated, show_scan=True),
    )


@router.callback_query(F.data == "rl:snimanual")
async def cb_rl_snimanual(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_sni)
    await cb.message.answer(
        "Пришли домен текстом (например <code>dl.google.com</code>), без "
        "https:// и порта. Проверю TLS1.3/серт перед применением.\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


async def _check_and_offer(message: Message, state: FSMContext, domain: str) -> None:
    domain = domain.strip().lower()
    if not domain or " " in domain or "/" in domain:
        await message.answer("Не похоже на домен. Отменено.")
        await state.clear()
        return
    await message.answer(f"Проверяю {html.escape(domain)}…")
    result = await reality_admin.check_sni_candidate(domain)
    await state.update_data(sni_domain=domain)
    if result["ok"]:
        text = (
            f"✅ <code>{html.escape(domain)}</code> годится: TLS {result['tls_version']}, "
            f"ALPN {html.escape(result['alpn'])}, издатель серта «{html.escape(result['issuer'])}».\n"
            f"Применить как новый SNI-камуфляж?"
        )
    else:
        text = (
            f"⚠️ <code>{html.escape(domain)}</code> не прошёл проверку: {html.escape(result['reason'])}.\n"
            f"Можно всё равно применить (на свой риск — REALITY может не заработать), или выбрать другой домен."
        )
    await message.answer(text, reply_markup=reality_sni_result_kb(result["ok"]))


@router.message(AdminFSM.reality_sni, F.text)
async def reality_sni_input(message: Message, state: FSMContext) -> None:
    await _check_and_offer(message, state, message.text or "")


@router.callback_query(F.data.startswith("rl:pick:"))
async def cb_rl_pick(cb: CallbackQuery, state: FSMContext) -> None:
    domain = cb.data.split(":", 2)[2]
    await state.set_state(AdminFSM.reality_sni)
    await cb.answer()
    await _check_and_offer(cb.message, state, domain)


@router.callback_query(F.data == "rl:sniapply")
async def cb_rl_sniapply(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    domain = data.get("sni_domain")
    if not domain:
        await cb.answer("Домен потерялся, начни заново", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(f"Применяю {html.escape(domain)}…")
    try:
        await reality_admin.set_sni(domain)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("reality set_sni failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ SNI-камуфляж теперь <code>{html.escape(domain)}</code>.")


@router.callback_query(F.data == "rl:scan")
async def cb_rl_scan(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_scan)
    await cb.message.answer(
        "🔍 Пришли цель для скана — IP, домен или подсеть не крупнее /24 "
        "(например <code>1.2.3.0/24</code>). Может занять до пары минут.\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.reality_scan, F.text)
async def reality_scan_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    target = (message.text or "").strip()
    if not target:
        await message.answer("Пусто. Отменено.")
        return
    await message.answer(f"Сканирую {html.escape(target)}…")
    try:
        results = await reality_scan.scan(target)
    except reality_scan.ScanError as e:
        await message.answer(f"❌ {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("reality_scan.scan failed")
        await message.answer("❌ Ошибка при сканировании. См. логи.")
        return
    if not results:
        await message.answer("Ничего подходящего не нашёл в этой цели.")
        return
    lines = [f"• <code>{html.escape(r['domain'])}</code> ({html.escape(r['geo'] or '—')})" for r in results]
    await message.answer(
        "🔍 Нашёл кандидатов:\n" + "\n".join(lines) + "\n\nВыбери, чтобы проверить и применить:",
        reply_markup=reality_scan_results_kb([r["domain"] for r in results], show_scan=True),
    )


@router.callback_query(F.data == "rl:keys")
async def cb_rl_keys(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🔑 Перегенерировать REALITY-ключи? Все текущие клиенты потеряют "
        "соединение немедленно (без грейс-периода) и подключатся заново "
        "на следующем обновлении подписки в приложении.",
        reply_markup=reality_confirm_kb("keys"),
    )
    await cb.answer()


@router.callback_query(F.data == "rl:shortids")
async def cb_rl_shortids(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🆔 Перегенерировать shortId? Все текущие клиенты потеряют "
        "соединение немедленно и подключатся заново на следующем "
        "обновлении подписки в приложении.",
        reply_markup=reality_confirm_kb("shortids"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:go:"))
async def cb_rl_go(cb: CallbackQuery) -> None:
    action = cb.data.split(":", 2)[2]
    await cb.answer()
    try:
        if action == "keys":
            pub = await reality_admin.regenerate_keys()
            await cb.message.answer(f"✅ Новые ключи применены. publicKey: <code>{html.escape(pub)}</code>")
        elif action == "shortids":
            ids = await reality_admin.regenerate_short_ids()
            await cb.message.answer(f"✅ Новые shortId применены ({len(ids)} шт.).")
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
    except Exception:  # noqa: BLE001
        log.exception("reality action %s failed", action)
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")


# ---------- shortId: точечное управление ----------

async def _show_sids(message: Message) -> None:
    try:
        s = await reality_admin.get_settings()
    except reality_admin.RealityError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    ids = s["short_ids"]
    await message.answer(
        f"🆔 shortId сейчас: <b>{len(ids)}</b> шт.\n"
        f"Тапни по конкретному, чтобы убрать его один (остальные не тронет), "
        f"или добавь новый.",
        reply_markup=reality_sids_kb(ids),
    )


@router.callback_query(F.data == "rl:sids")
async def cb_rl_sids(cb: CallbackQuery) -> None:
    await _show_sids(cb.message)
    await cb.answer()


@router.callback_query(F.data == "rl:sidadd")
async def cb_rl_sidadd(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        new_id = await reality_admin.add_short_id()
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_sidadd failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ Добавлен shortId <code>{html.escape(new_id)}</code>, остальные не тронуты.")
    await _show_sids(cb.message)


@router.callback_query(F.data.startswith("rl:sidrm:"))
async def cb_rl_sidrm(cb: CallbackQuery) -> None:
    short_id = cb.data.split(":", 2)[2]
    await cb.answer()
    try:
        await reality_admin.remove_short_id(short_id)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_sidrm failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ shortId <code>{html.escape(short_id)}</code> убран.")
    await _show_sids(cb.message)


# ---------- порт REALITY-инбаунда ----------

_PORT_WARNING = (
    "⚠️ Меняет порт на весь кластер сразу, но <b>UFW на уже развёрнутых "
    "нодах эта команда не трогает</b> — новый порт там нужно открыть "
    "руками (node/bootstrap.sh, INBOUND_PORTS), иначе клиенты просто не "
    "достучатся до ноды по новому порту."
)


async def _apply_port(message: Message, port: int) -> None:
    try:
        await reality_admin.set_port(port)
    except reality_admin.RealityError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("_apply_port failed")
        await message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await message.answer(
        f"✅ Порт теперь <b>{port}</b>. Не забудь открыть его в UFW на всех "
        f"уже развёрнутых нодах."
    )


@router.callback_query(F.data == "rl:port")
async def cb_rl_port(cb: CallbackQuery) -> None:
    await cb.message.answer(
        f"🔌 Выбери порт или впиши свой.\n\n{_PORT_WARNING}",
        reply_markup=reality_port_picker_kb(reality_admin.SUGGESTED_PORTS),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:portpick:"))
async def cb_rl_portpick(cb: CallbackQuery) -> None:
    port = int(cb.data.split(":", 2)[2])
    await cb.answer()
    await _apply_port(cb.message, port)


@router.callback_query(F.data == "rl:portmanual")
async def cb_rl_portmanual(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_port)
    await cb.message.answer("Пришли порт текстом (число 1-65535).\nОтмена — любая кнопка снизу.")
    await cb.answer()


@router.message(AdminFSM.reality_port, F.text)
async def reality_port_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Не похоже на число. Отменено.")
        return
    await _apply_port(message, int(text))


# ---------- SpiderX ----------

async def _apply_spx(message: Message, value: str) -> None:
    try:
        await reality_admin.set_spx(value)
    except reality_admin.RealityError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("_apply_spx failed")
        await message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    if value:
        await message.answer(f"✅ SpiderX теперь <code>{html.escape(value)}</code>.")
    else:
        await message.answer("✅ SpiderX сброшен на дефолт.")


@router.callback_query(F.data == "rl:spx")
async def cb_rl_spx(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🕸 Путь в фейковом запросе к сайту-камуфляжу — выбери, накинь "
        "случайный или впиши свой.",
        reply_markup=reality_spx_picker_kb(reality_admin.SPX_SUGGESTIONS),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:spxpick:"))
async def cb_rl_spxpick(cb: CallbackQuery) -> None:
    path = cb.data.split(":", 2)[2]
    await cb.answer()
    await _apply_spx(cb.message, path)


@router.callback_query(F.data == "rl:spxrandom")
async def cb_rl_spxrandom(cb: CallbackQuery) -> None:
    await cb.answer()
    await _apply_spx(cb.message, reality_admin.random_spx())


@router.callback_query(F.data == "rl:spxmanual")
async def cb_rl_spxmanual(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_spx)
    await cb.message.answer(
        "Пришли SpiderX-путь текстом (например <code>/url</code>). Пустое "
        "сообщение (пробел) сбрасывает на дефолт.\nОтмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.reality_spx, F.text)
async def reality_spx_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _apply_spx(message, (message.text or "").strip())


# ---------- фингерпринт разом на все ноды ----------

@router.callback_query(F.data == "rl:fp")
async def cb_rl_fp(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🫆 Фингерпринт для ВСЕХ серверов разом (у каждого по отдельности — "
        "в карточке сервера, «🌍 Серверы»). «randomized» — свой отпечаток на "
        "каждое соединение, надёжнее одного фиксированного значения на весь флот.",
        reply_markup=reality_fp_picker_kb(reality_admin.FINGERPRINT_OPTIONS),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:fppick:"))
async def cb_rl_fppick(cb: CallbackQuery) -> None:
    fp = cb.data.split(":", 2)[2]
    await cb.answer()
    try:
        changed = await reality_admin.set_fingerprint_all(fp)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_fppick failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ Фингерпринт <code>{html.escape(fp)}</code> применён на {changed} серверах.")


# ---------- WARP (ручная маршрутизация отдельных доменов) ----------

def _warp_tag(address: str) -> str:
    """Тег outbound'а детерминированно выводится из IP — не нужна отдельная
    таблица «нода -> тег», просто пересчитывается каждый раз."""
    return "warp-" + address.replace(".", "-").replace(":", "-")


async def _warp_nodes_screen(cb: CallbackQuery) -> None:
    try:
        nodes = await node_provision.list_nodes()
        outbounds = await warp_admin.list_warp_outbounds()
    except (node_provision.ProvisionError, warp_admin.WarpAdminError) as e:
        await cb.message.edit_text(f"❌ Не удалось получить данные: {e}")
        return
    tags = {o["tag"] for o in outbounds}
    items = [(n["address"], n.get("name") or n["address"], _warp_tag(n["address"]) in tags) for n in nodes]
    if not items:
        await cb.message.edit_text("Нод нет.")
        return
    await cb.message.edit_text(
        "🌐 <b>WARP</b>\nОтдельная identity на ноду (см. обсуждение в чате — общий "
        "core config, полной изоляции по нодам это не даёт, но снижает риск). "
        "Выбери ноду:",
        reply_markup=warp_nodes_kb(items),
    )


@router.callback_query(F.data == "rl:warp")
async def cb_rl_warp(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await _warp_nodes_screen(cb)


async def _warp_node_screen(cb: CallbackQuery, address: str) -> None:
    tag = _warp_tag(address)
    try:
        outbounds = await warp_admin.list_warp_outbounds()
    except warp_admin.WarpAdminError as e:
        await cb.message.edit_text(f"❌ Не удалось получить данные: {e}")
        return
    registered = any(o["tag"] == tag for o in outbounds)
    domains: list[str] = []
    if registered:
        try:
            domains = await warp_admin.list_warp_routes(tag)
        except warp_admin.WarpAdminError as e:
            await cb.message.answer(f"⚠️ Не смог получить список доменов: {e}")
    status = "✅ зарегистрирован" if registered else "⚪️ не зарегистрирован"
    dom_line = ("\n".join(f"• <code>{html.escape(d)}</code>" for d in domains) or "—") if registered else "—"
    await cb.message.edit_text(
        f"🌐 <b>WARP — {html.escape(address)}</b>\n"
        f"Статус: {status}\n"
        f"Домены через WARP:\n{dom_line}",
        reply_markup=warp_node_kb(address, registered, domains),
    )


@router.callback_query(F.data.startswith("rl:warp:node:"))
async def cb_rl_warp_node(cb: CallbackQuery) -> None:
    address = cb.data.split(":", 3)[3]
    await cb.answer()
    await _warp_node_screen(cb, address)


@router.callback_query(F.data.startswith("rl:warp:reg:"))
async def cb_rl_warp_reg(cb: CallbackQuery) -> None:
    address = cb.data.split(":", 3)[3]
    await cb.answer("Регистрирую в Cloudflare…")
    try:
        nodes = await node_provision.list_nodes()
        name = next((n.get("name") for n in nodes if n["address"] == address), address)
        data = await warp.register(name)
        await warp_admin.add_warp_outbound(_warp_tag(address), data)
    except (warp.WarpError, warp_admin.WarpAdminError, node_provision.ProvisionError) as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_warp_reg failed")
        await cb.message.answer("❌ Ошибка. См. логи.")
        return
    await _warp_node_screen(cb, address)


@router.callback_query(F.data.startswith("rl:warp:del:"))
async def cb_rl_warp_del(cb: CallbackQuery) -> None:
    address = cb.data.split(":", 3)[3]
    await cb.answer()
    try:
        await warp_admin.remove_warp_outbound(_warp_tag(address))
    except warp_admin.WarpAdminError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    await _warp_node_screen(cb, address)


@router.callback_query(F.data.startswith("rl:warp:domadd:"))
async def cb_rl_warp_domadd(cb: CallbackQuery, state: FSMContext) -> None:
    address = cb.data.split(":", 3)[3]
    await state.clear()
    categories = [name for name, _ in warp_admin.WARP_DOMAIN_PRESETS]
    await cb.message.edit_text(
        "Через какую категорию доменов? Или свой домен вручную.",
        reply_markup=warp_domadd_categories_kb(address, categories),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:warp:domcat:"))
async def cb_rl_warp_domcat(cb: CallbackQuery) -> None:
    _, _, _, address, idx_s = cb.data.split(":", 4)
    await cb.answer()
    cat_idx = int(idx_s)
    if not (0 <= cat_idx < len(warp_admin.WARP_DOMAIN_PRESETS)):
        await cb.message.answer("Категория не найдена, начни заново.")
        return
    name, domains = warp_admin.WARP_DOMAIN_PRESETS[cat_idx]
    try:
        already = await warp_admin.list_warp_routes(_warp_tag(address))
    except warp_admin.WarpAdminError as e:
        await cb.message.answer(f"❌ Не удалось получить текущие домены: {e}")
        already = []
    await cb.message.edit_text(
        f"{name} — выбери домен (уже добавленные помечены):",
        reply_markup=warp_domadd_domains_kb(address, cat_idx, domains, already),
    )


@router.callback_query(F.data.startswith("rl:warp:dompick:"))
async def cb_rl_warp_dompick(cb: CallbackQuery) -> None:
    _, _, _, address, cat_idx_s, dom_idx_s = cb.data.split(":", 5)
    await cb.answer()
    cat_idx, dom_idx = int(cat_idx_s), int(dom_idx_s)
    if not (0 <= cat_idx < len(warp_admin.WARP_DOMAIN_PRESETS)):
        await cb.message.answer("Категория не найдена, начни заново.")
        return
    _, domains = warp_admin.WARP_DOMAIN_PRESETS[cat_idx]
    if not (0 <= dom_idx < len(domains)):
        await cb.message.answer("Домен не найден, начни заново.")
        return
    domain = domains[dom_idx]
    try:
        await warp_admin.add_warp_route(_warp_tag(address), domain)
    except warp_admin.WarpAdminError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    await cb.message.answer(f"✅ <code>{html.escape(domain)}</code> теперь идёт через WARP этой ноды.")
    await _warp_node_screen(cb, address)


@router.callback_query(F.data.startswith("rl:warp:domcatall:"))
async def cb_rl_warp_domcatall(cb: CallbackQuery) -> None:
    _, _, _, address, idx_s = cb.data.split(":", 4)
    await cb.answer()
    cat_idx = int(idx_s)
    if not (0 <= cat_idx < len(warp_admin.WARP_DOMAIN_PRESETS)):
        await cb.message.answer("Категория не найдена, начни заново.")
        return
    name, domains = warp_admin.WARP_DOMAIN_PRESETS[cat_idx]
    try:
        added = await warp_admin.add_warp_route_bulk(_warp_tag(address), domains)
    except warp_admin.WarpAdminError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    await cb.message.answer(f"✅ {name}: добавлено {added} домен(ов) (из {len(domains)}).")
    await _warp_node_screen(cb, address)


@router.callback_query(F.data.startswith("rl:warp:dommanual:"))
async def cb_rl_warp_dommanual(cb: CallbackQuery, state: FSMContext) -> None:
    address = cb.data.split(":", 3)[3]
    await state.set_state(AdminFSM.warp_domain)
    await state.update_data(warp_address=address)
    await cb.message.answer(
        "Пришли домен, который пускать через WARP этой ноды (например "
        "<code>example.com</code>). Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.warp_domain, F.text)
async def warp_domain_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    address = data.get("warp_address")
    domain = (message.text or "").strip().lower()
    if not address:
        await message.answer("Сессия истекла, начни заново из меню WARP.")
        return
    if not warp_admin.valid_domain(domain):
        await message.answer(f"«{html.escape(domain)}» не похож на домен. Отменено.")
        return
    try:
        await warp_admin.add_warp_route(_warp_tag(address), domain)
    except warp_admin.WarpAdminError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    await message.answer(f"✅ <code>{html.escape(domain)}</code> теперь идёт через WARP этой ноды.")


@router.callback_query(F.data.startswith("rl:warp:domrm:"))
async def cb_rl_warp_domrm(cb: CallbackQuery) -> None:
    _, _, _, address, idx_s = cb.data.split(":", 4)
    await cb.answer()
    tag = _warp_tag(address)
    try:
        domains = await warp_admin.list_warp_routes(tag)
        idx = int(idx_s)
        if 0 <= idx < len(domains):
            await warp_admin.remove_warp_route(tag, domains[idx])
    except warp_admin.WarpAdminError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    await _warp_node_screen(cb, address)


# ---------- утилиты ----------

async def _safe_user_msg(tg_id: int, text: str, reply_markup=None) -> None:
    try:
        await get_bot().send_message(tg_id, text, reply_markup=reply_markup)
    except Exception as e:  # noqa: BLE001
        log.warning("Не доставлено пользователю %s: %s", tg_id, e)
