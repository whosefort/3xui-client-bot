"""Admin operations explicitly scoped to a 3x-ui panel and an inbound."""
from __future__ import annotations

import hashlib
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4
from collections.abc import Iterable
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db, runtime
from ..config import config
from ..keyboards import ADM_XUI
from ..panels import xui_bootstrap, xui_registry
from ..panels.base import PanelError
from ..panels.xui_infra import add_reality_preset
from ..panels.ownership import create_named_client, ensure_panel_owner, subscription_targets
from ..panels.xui import XUIClient
from ..panels.xui_routes import client_route, exit_outbound, outbound_tags, set_client_route
from .common import BindingConflict, bind_client, binding_lock, get_traffic_gb

router = Router(name="xui_admin")
router.message.filter(F.from_user.id.in_(config.admin_ids), F.chat.type == "private")
router.callback_query.filter(F.from_user.id.in_(config.admin_ids), F.message.chat.type == "private")
log = logging.getLogger(__name__)

PAGE_SIZE = 12
CLIENT_ACTIONS = {
    "renew": "Продлить подписку",
    "unlimited": "Сделать бессрочной и безлимитной",
    "reset": "Сбросить счётчик трафика",
    "delete": "Удалить клиента с панели",
}


class XUIInput(StatesGroup):
    create = State()
    bind = State()
    hwid_limit = State()
    register = State()
    address = State()
    bootstrap = State()


def _key(text: str) -> str:
    return hashlib.blake2s(text.encode(), digest_size=8).hexdigest()


def _keyboard(rows: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label[:64], callback_data=callback if callback.startswith("xi:") else f"xu:{callback}")]
        for label, callback in rows
    ])


def _panel(key: str) -> tuple[str, XUIClient]:
    try:
        return runtime.xui_panels[key]
    except KeyError as e:
        raise PanelError("Панель больше не подключена.") from e


async def _inbound(client: XUIClient, number: int) -> dict:
    inbound = next((item for item in await client.list_inbounds() if item["id"] == number), None)
    if inbound is None:
        raise PanelError("Этот вход больше не существует.")
    return inbound


def _clients(inbound: dict) -> list[dict]:
    return sorted((c for c in inbound["settings"].get("clients", []) if c.get("email")),
                  key=lambda c: c["email"].casefold())


def _matching(items: Iterable[str], key: str) -> str:
    matches = [item for item in items if _key(item) == key]
    if len(matches) != 1:
        raise PanelError("Эта кнопка устарела. Откройте карточку заново.")
    return matches[0]


async def _show(message: Message, text: str, rows: Iterable[tuple[str, str]]) -> None:
    await message.edit_text(text, reply_markup=_keyboard(rows))


def _panel_rows() -> list[tuple[str, str]]:
    return ([(name, f"p:{key}:0") for key, (name, _) in runtime.xui_panels.items()]
            + [("Подключить готовую панель", "register"), ("Настроить чистый VPS", "bootstrap")])


@router.message(Command("xui"))
@router.message(F.text == ADM_XUI)
async def show_panels(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🖥 <b>Панели 3x-ui</b>", reply_markup=_keyboard(_panel_rows()))


async def _show_panel(message: Message, key: str, page: int) -> None:
    name, panel = _panel(key)
    status = await panel.server_status()
    xray = status.get("xray") or {}
    inbounds = sorted(await panel.list_inbounds(), key=lambda x: (x.get("port", 0), x["id"]))
    start = max(0, min(page * PAGE_SIZE, ((len(inbounds) - 1) // PAGE_SIZE) * PAGE_SIZE))
    rows = [(f"{'🟢' if i.get('enable') else '⚪️'} {i.get('remark') or i['protocol']} · {i['port']}",
             f"i:{key}:{i['id']}:0") for i in inbounds[start:start + PAGE_SIZE]]
    if start:
        rows.append(("← Предыдущие входы", f"p:{key}:{start // PAGE_SIZE - 1}"))
    if start + PAGE_SIZE < len(inbounds):
        rows.append(("Следующие входы →", f"p:{key}:{start // PAGE_SIZE + 1}"))
    rows += [("Ноды", f"xi:nodes:{key}"), ("Исходящие и тесты", f"xi:out:{key}"),
             ("➕ Вход по пресету", f"xi:presets:{key}:in"),
             ("Адрес панели", f"addr:{key}"), ("Резервная копия", f"xi:backup:{key}"),
             ("← Панели", "home")]
    await _show(message, f"<b>{html.escape(name)}</b>\nXray {html.escape(str(xray.get('version') or ''))}"
                f" · {html.escape(str(xray.get('state') or 'неизвестно'))}\n"
                f"Входов: {len(inbounds)}", rows)


async def _show_inbound(message: Message, key: str, inbound: dict, page: int) -> None:
    number = inbound["id"]
    clients = _clients(inbound)
    start = max(0, min(page * PAGE_SIZE, ((len(clients) - 1) // PAGE_SIZE) * PAGE_SIZE))
    back = f"i:{key}:{number}"
    rows = [(f"{'🟢' if c.get('enable') else '⚪️'} {c['email']}",
             f"c:{key}:{number}:{_key(c['email'])}") for c in clients[start:start + PAGE_SIZE]]
    if start:
        rows.append(("← Предыдущие клиенты", f"{back}:{start // PAGE_SIZE - 1}"))
    if start + PAGE_SIZE < len(clients):
        rows.append(("Следующие клиенты →", f"{back}:{start // PAGE_SIZE + 1}"))
    if inbound["protocol"] == "vless" and inbound.get("enable") and not inbound.get("excludeFromSub"):
        rows.append(("➕ Создать VLESS-профиль", f"new:{key}:{number}"))
        if key in subscription_targets():
            rows.append(("Создать приглашение без Telegram ID", f"invite:{key}:{number}"))
    transport = inbound["streamSettings"]
    if (inbound["protocol"] == "vless" and transport.get("security") == "reality"
            and transport.get("network") in ("raw", "tcp")):
        rows.append(("Настроить dest и SNI", f"xi:reality:{key}:{number}"))
    enabled = bool(inbound.get("enable"))
    rows.append(("Выключить вход" if enabled else "Включить вход",
                 f"xi:it:{key}:{number}:{int(not enabled)}"))
    rows.append(("← Панель", f"p:{key}:0"))
    title = inbound.get("remark") or inbound["protocol"]
    await _show(message, f"<b>{html.escape(str(title))}</b> · {inbound['port']}\n"
                f"{html.escape(str(inbound['protocol']))} / "
                f"{html.escape(str(transport.get('network') or '—'))} / "
                f"{html.escape(str(transport.get('security') or '—'))}\nКлиентов: {len(clients)}", rows)


async def _show_client(message: Message, key: str, inbound: dict, email: str) -> None:
    _, panel = _panel(key)
    client = await panel.find_by_username(email)
    if client is None:
        raise PanelError("Клиент больше не существует.")
    template, _ = await panel.xray_template()
    tags = outbound_tags(template)
    routes = [r["outboundTag"] for r in template.get("routing", {}).get("rules", [])
              if client_route(r, inbound["tag"], email, tags)]
    group = f"{key}:{inbound['id']}:{_key(email)}"
    rows = [
        ("Ссылки подключения", f"links:{group}"),
        ("Подписка и лимит", f"plan:{group}"),
        ("Устройства и HWID", f"hw:{group}"),
        ("Назначить выход", f"route:{group}"),
        ("Включить на панели" if not client.enabled else "Приостановить на панели",
         f"toggle:{group}:{int(not client.enabled)}"),
        ("← Клиенты входа", f"i:{key}:{inbound['id']}:0"),
    ]
    if key in subscription_targets():
        rows.insert(1, ("Привязать Telegram ID", f"bind:{group}"))
    exits = ", ".join(routes) if routes else "по правилам панели"
    days = client.days_left
    expiry = "бессрочно" if days is None else f"{days} дн."
    limit = f" / {client.limit_bytes / 1024**2:.1f} МБ" if client.limit_bytes else ""
    await _show(message, f"<b>{html.escape(email)}</b>\n"
                f"Состояние: {html.escape(client.access_status)} · {expiry}\n"
                f"Трафик: {client.used_bytes / 1024**2:.1f} МБ{limit}\n"
                f"Выход: {html.escape(exits)}", rows)


async def _show_hwids(message: Message, panel: XUIClient, email: str, group: str, page: int = 0) -> None:
    client = await panel.find_by_username(email)
    if client is None:
        raise PanelError("Клиент больше не существует.")
    devices = await panel.hwids(email)
    maximum = int(client.raw.get("limitHwid") or 0)
    start = max(0, min(page, (max(len(devices), 1) - 1) // PAGE_SIZE)) * PAGE_SIZE
    lines = [f"<b>{html.escape(email)} · Устройства</b>",
             f"Зарегистрировано: {len(devices)} · лимит: {maximum or 'без лимита'}"]
    rows = []
    for device in devices[start:start + PAGE_SIZE]:
        model = str(device.get("deviceModel") or device.get("deviceOs") or "Устройство")[:40]
        fingerprint = str(device.get("fingerprint") or "")[:12]
        date = "—"
        seen = device.get("lastSeen")
        if isinstance(seen, (float, int)) and 0 < seen < 4_102_444_800_000:
            date = datetime.fromtimestamp(seen / 1000, ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M МСК")
        lines.append(f"#{device['id']} {html.escape(model)} {html.escape(fingerprint)} · {date}")
        rows.append((f"Отвязать #{device['id']} · {model}", f"hf:{group}:{device['id']}"))
    if start:
        rows.append(("← Предыдущие устройства", f"hw:{group}:{start // PAGE_SIZE - 1}"))
    if start + PAGE_SIZE < len(devices):
        rows.append(("Следующие устройства →", f"hw:{group}:{start // PAGE_SIZE + 1}"))
    if devices:
        rows.append(("Отвязать все", f"hf:{group}:0"))
    rows += [("Лимит устройств", f"hl:{group}"), ("← Клиент", f"c:{group}")]
    await _show(message, "\n".join(lines), rows)


@router.callback_query(F.data.startswith("xu:"))
async def manage(query: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(query.message, Message):
        await query.answer()
        return
    await query.answer()
    message = query.message
    action, *args = query.data.split(":")[1:]
    try:
        if action == "home":
            await state.clear()
            await _show(message, "🖥 <b>Панели 3x-ui</b>", _panel_rows())
            return
        if action == "register":
            await state.set_state(XUIInput.register)
            await message.answer(
                "Пришлите пять строк: идентификатор панели, название, HTTPS-адрес панели, "
                "HTTPS-шаблон подписки с /{subId} в конце и имя переменной XUI_…_TOKEN. "
                "Токен задайте в окружении бота. Отмена — /cancel."
            )
            return
        if action == "bootstrap":
            await state.set_state(XUIInput.bootstrap)
            await message.answer(
                "Пришлите в одной строке идентификатор, IPv4, SSH-порт и DNS-имя "
                "чистого VPS на Debian/Ubuntu. Например <code>newnode 203.0.113.10 22 "
                "vpn.example.org</code>. VPS должен принимать SSH-ключ установщика. "
                "Бот установит 3x-ui, создаст VLESS-вход и одноразовое приглашение. "
                "Отмена — /cancel."
            )
            return
        key, *args = args
        name, panel = _panel(key)
        if action == "addr":
            url = urlsplit(panel.base)
            address = url._replace(netloc=url.netloc.rsplit("@", 1)[-1], query="", fragment="").geturl()
            await _show(message, f"<b>{html.escape(name)} · Адрес панели</b>\n"
                        f"<code>{html.escape(address)}</code>", [("Сменить адрес в боте", f"setaddr:{key}"),
                                                               ("← Панель", f"p:{key}:0")])
            return
        if action == "setaddr":
            await state.set_state(XUIInput.address)
            await state.update_data(xui_key=key)
            await message.answer("Пришлите новый HTTPS-адрес этой же панели. "
                                 "Пока старый адрес работает, бот сверит список подписок. Отмена — /cancel.")
            return
        if action == "p":
            await _show_panel(message, key, int(args[0]))
            return
        if action == "o":
            template, _ = await panel.xray_template()
            tags = outbound_tags(template)
            rows = [f"• <code>{html.escape(tag)}</code>" for tag in tags[:30]]
            await _show(message, f"<b>{html.escape(name)} · Исходящие</b>\n" +
                        ("\n".join(rows) if rows else "Нет доступных выходов."),
                        [("← Панель", f"p:{key}:0")])
            return
        number, *args = args
        inbound = await _inbound(panel, int(number))
        if action == "i":
            await state.update_data(xui_invite=None)
            await _show_inbound(message, key, inbound, int(args[0]))
            return
        if action == "new":
            await state.set_state(XUIInput.create)
            await state.update_data(xui_key=key, inbound_id=int(number))
            await message.answer("Пришлите Telegram ID клиента. Бот создаст профиль на выбранном входе "
                                 "по текущему тарифу. Для отмены отправьте /cancel.")
            return
        if action == "invite":
            if key not in subscription_targets():
                raise PanelError("Для этой панели не настроена ссылка подписки.")
            await _show(message, "Создать VLESS-клиента и одноразовое приглашение?",
                        [("Создать", f"issue:{key}:{number}"), ("Отмена", f"i:{key}:{number}:0")])
            await state.update_data(xui_invite=[message.message_id, key, number])
            return
        if action == "issue":
            if key not in subscription_targets():
                raise PanelError("Для этой панели не настроена ссылка подписки.")
            async with binding_lock, panel.config_lock:
                if (await state.get_data()).get("xui_invite") != [message.message_id, key, number]:
                    raise PanelError("Подтверждение уже использовано.")
                await state.update_data(xui_invite=None)
                client = await create_named_client(panel, 0, config.plan_days, get_traffic_gb(), int(number),
                                                   username="client_" + uuid4().hex[:24])
                token = db.create_client_invite(client.username, key)
            bot_name = (await runtime.get_bot().me()).username
            await message.answer(f"Одноразовое приглашение на 24 часа\n"
                                 f"<code>https://t.me/{bot_name}?start=claim_{token}</code>")
            await _show_inbound(message, key, await _inbound(panel, int(number)), 0)
            return
        client_key, *args = args
        email = _matching((c["email"] for c in _clients(inbound)), client_key)
        group = f"{key}:{number}:{client_key}"
        if action == "c":
            await _show_client(message, key, inbound, email)
        elif action == "links":
            links = await panel.client_links(email)
            if not links:
                await message.answer("3x-ui не вернула ссылок для этого клиента.")
            for link in links[:10]:
                if len(link) <= 3500:
                    await message.answer(f"<code>{html.escape(link)}</code>", protect_content=True)
        elif action == "bind":
            if key not in subscription_targets():
                raise PanelError("Для панели не настроена ссылка подписки.")
            await state.set_state(XUIInput.bind)
            await state.update_data(xui_key=key, inbound_id=int(number), xui_email=email)
            await message.answer("Пришлите Telegram ID для привязки. Отмена — /cancel.")
        elif action == "hw":
            await state.update_data(hwid_forget=None)
            await _show_hwids(message, panel, email, group, int(args[0]) if args else 0)
        elif action == "hl":
            await state.set_state(XUIInput.hwid_limit)
            await state.update_data(xui_key=key, inbound_id=int(number), xui_email=email)
            await message.answer("Пришлите лимит устройств. 0 — без лимита. Отмена — /cancel.")
        elif action == "hf":
            device = int(args[0])
            await state.update_data(hwid_forget=[message.message_id, group, device])
            what = f"устройство #{device}" if device else "все устройства"
            await _show(message, f"Отвязать {what} у <b>{html.escape(email)}</b>?",
                        [("Подтвердить", f"hd:{group}:{device}"), ("Отмена", f"hw:{group}")])
        elif action == "hd":
            device = int(args[0])
            if (await state.get_data()).get("hwid_forget") != [message.message_id, group, device]:
                raise PanelError("Подтверждение больше не действует.")
            await state.update_data(hwid_forget=None)
            await panel.forget_hwids(email, device or None)
            await _show_hwids(message, panel, email, group)
        elif action == "plan":
            await state.update_data(xui_operation=None)
            rows = [(label, f"op:{group}:{op}") for op, label in CLIENT_ACTIONS.items()]
            await _show(message, f"<b>{html.escape(email)}</b>\nДействия с подпиской на этой панели",
                        [*rows, ("← Клиент", f"c:{group}")])
        elif action in ("op", "do"):
            op = args[0]
            label = CLIENT_ACTIONS[op]
            if action == "op":
                if op == "renew":
                    label += f" на {config.plan_days} дней"
                callback = f"do:{group}:{op}"
                await state.update_data(xui_operation=[message.message_id, callback])
                await _show(message, f"<b>{html.escape(name)}</b> · {html.escape(email)}\n"
                            f"{label}?",
                            [("Подтвердить", callback), ("Отмена", f"plan:{group}")])
                return
            async with panel.config_lock:
                pending = (await state.get_data()).get("xui_operation")
                if pending != [message.message_id, f"do:{group}:{op}"]:
                    raise PanelError("Подтверждение уже использовано. Откройте действие заново.")
                await state.update_data(xui_operation=None)
                client = await panel.find_by_username(email)
                if client is None:
                    raise PanelError("Клиент больше не существует.")
                if op == "renew":
                    await panel.extend_client(client=client, add_days=config.plan_days)
                elif op == "unlimited":
                    await panel.set_unlimited(client=client, reset_traffic=False)
                elif op == "reset":
                    await panel.reset_traffic(client=client)
                else:
                    await panel.delete_client(email)
                    db.clear_panel_client(key, email)
            if op == "delete":
                await message.answer("Клиент удалён с этой панели.")
                await _show_inbound(message, key, await _inbound(panel, int(number)), 0)
            else:
                await _show_client(message, key, inbound, email)
        elif action in ("route", "confirm", "apply"):
            template, _ = await panel.xray_template()
            tags = outbound_tags(template)
            if action == "route":
                rows = [(tag, f"confirm:{group}:{_key(tag)}") for tag in tags[:40]]
                rows += [(f"Через панель: {name}", f"bridge:{group}:{node}")
                         for node, (name, other) in runtime.xui_panels.items()
                         if node != key and other.exit_client]
                rows += [("Обычные правила", f"confirm:{group}:-"), ("← Клиент", f"c:{group}")]
                await _show(message, "Выберите исходящий маршрут для этого клиента.", rows)
            else:
                target = None if args[0] == "-" else _matching(tags, args[0])
                if action == "confirm":
                    label = html.escape(target or "обычные правила")
                    await _show(message, f"<b>{html.escape(name)}</b> · {html.escape(email)} → {label}\n"
                                "Чтобы применить правку, Xray кратковременно перезапустится.",
                                [("Применить", f"apply:{group}:{args[0]}"),
                                 ("Отмена", f"c:{group}")])
                else:
                    changed = await set_client_route(panel, int(number), email, target)
                    await message.answer("Правило применено и проверено." if changed else "Правило уже актуально.")
                    await _show_client(message, key, inbound, email)
        elif action in ("bridge", "connect"):
            other_key = args[0]
            if other_key == key:
                raise PanelError("Панель выхода должна отличаться от входной.")
            other_name, other = _panel(other_key)
            if action == "bridge":
                await _show(message, f"<b>{html.escape(name)}</b> · {html.escape(email)} → "
                            f"{html.escape(other_name)}\n"
                            "Бот возьмёт ссылку выделенного клиента из панели выхода. "
                            "На входной панели Xray кратковременно перезапустится.",
                            [("Применить", f"connect:{group}:{other_key}"),
                             ("Отмена", f"c:{group}")])
            else:
                out = await exit_outbound(other, other_key)
                changed = await set_client_route(panel, int(number), email, out["tag"], outbound=out)
                await message.answer("Маршрут сохранён и проверен." if changed else "Маршрут уже настроен.")
                await _show_client(message, key, inbound, email)
        elif action == "toggle":
            client = await panel.find_by_username(email)
            if client is None:
                raise PanelError("Клиент больше не существует.")
            enabled = args[0] == "1"
            if enabled != client.enabled:
                await panel.set_enabled(client=client, enabled=enabled)
            await _show_client(message, key, inbound, email)
    except PanelError as e:
        await message.answer(html.escape(str(e)))
    except (ValueError, KeyError, IndexError, TypeError):
        log.warning("Invalid 3x-ui response or obsolete callback", exc_info=False)
        await message.answer("Откройте /xui и повторите операцию.")


@router.message(XUIInput.bootstrap, Command("cancel"))
@router.message(XUIInput.address, Command("cancel"))
@router.message(XUIInput.register, Command("cancel"))
@router.message(XUIInput.create, Command("cancel"))
@router.message(XUIInput.bind, Command("cancel"))
@router.message(XUIInput.hwid_limit, Command("cancel"))
async def cancel_creation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Операция отменена.")


@router.message(XUIInput.create, F.text.regexp(r"^[0-9]{1,16}$"))
async def create_client(message: Message, state: FSMContext) -> None:
    tg_id = int(message.text)
    if tg_id <= 0 or tg_id >= 2 ** 52:
        await message.answer("Telegram ID должен быть положительным.")
        return
    data = await state.get_data()
    try:
        _, panel = _panel(data["xui_key"])
        inbound = await _inbound(panel, data["inbound_id"])
        if inbound["protocol"] != "vless" or not inbound.get("enable"):
            raise PanelError("Выбранный VLESS-вход больше не доступен.")
        email = panel.username_for(tg_id)
        key = data["xui_key"]
        async with binding_lock, panel.config_lock:
            if key in subscription_targets():
                await ensure_panel_owner(tg_id, key)
            if any(c.username == email or c.tg_id == tg_id for c in await panel.list_clients(force=True)):
                raise PanelError("На этой панели уже есть такой клиент.")
            client = await create_named_client(panel, tg_id, config.plan_days, get_traffic_gb(), inbound["id"])
            if key in subscription_targets():
                db.save_client_binding(tg_id, None, client.username, client.sub_url, key)
        await state.clear()
        await message.answer(f"Клиент <code>{html.escape(client.username)}</code> создан.",
                             reply_markup=_keyboard([("Открыть клиента",
                                 f"c:{data['xui_key']}:{inbound['id']}:{_key(client.username)}")]))
    except PanelError as e:
        await message.answer(html.escape(str(e)))


@router.message(XUIInput.bind, F.text.regexp(r"^[0-9]{1,16}$"))
async def bind_named(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    tg_id = int(message.text)
    if tg_id <= 0 or tg_id >= 2 ** 52:
        await message.answer("Нужен корректный положительный Telegram ID.")
        return
    try:
        key = data["xui_key"]
        _, panel = _panel(key)
        if key not in subscription_targets():
            raise PanelError("Для панели не настроена ссылка подписки.")
        async with binding_lock:
            inbound = await _inbound(panel, data["inbound_id"])
            email = data["xui_email"]
            if not any(c["email"] == email for c in _clients(inbound)):
                raise PanelError("Клиент больше не привязан к этому входу.")
            client = await panel.find_by_username(email)
            if client is None:
                raise PanelError("Клиент больше не существует.")
            await bind_client(panel, client, tg_id)
        await state.clear()
        await message.answer("Telegram ID привязан. Подписка доступна клиенту через бота.")
    except (PanelError, BindingConflict) as e:
        await message.answer(html.escape(str(e)))


@router.message(XUIInput.hwid_limit, F.text.regexp(r"^[0-9]{1,5}$"))
async def hwid_limit_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        key = data["xui_key"]
        _, panel = _panel(key)
        inbound = await _inbound(panel, data["inbound_id"])
        email = data["xui_email"]
        if not any(c["email"] == email for c in _clients(inbound)):
            raise PanelError("Клиент больше не привязан к этому входу.")
        value = int(message.text)
        await panel.set_hwid_limit(email, value)
        await state.clear()
        group = f"{key}:{inbound['id']}:{_key(email)}"
        await message.answer(f"Лимит устройств: {value or 'без лимита'}.",
                             reply_markup=_keyboard([("Устройства и HWID", f"hw:{group}")]))
    except PanelError as e:
        await message.answer(html.escape(str(e)))


@router.message(XUIInput.register, F.text)
async def register_panel(message: Message, state: FSMContext) -> None:
    values = message.text.strip().splitlines()
    if len(values) != 5:
        await message.answer("Нужно пять строк. Для отмены отправьте /cancel.")
        return
    key, name, url, subscription, token_env = (value.strip() for value in values)
    try:
        await xui_registry.register(key, name, url, subscription, token_env)
    except PanelError as exc:
        await message.answer(html.escape(str(exc)))
        return
    await state.clear()
    await message.answer("Панель подключена. Выберите существующий инбаунд или создайте вход по пресету.",
                         reply_markup=_keyboard([("Открыть панель", f"p:{key}:0")]))


@router.message(XUIInput.address, F.text)
async def update_panel_address(message: Message, state: FSMContext) -> None:
    key = (await state.get_data()).get("xui_key")
    if not key:
        await state.clear()
        return
    try:
        async with binding_lock:
            await xui_registry.change_address(key, message.text.strip())
    except PanelError as exc:
        await message.answer(html.escape(str(exc)))
        return
    await state.clear()
    await message.answer("Новый адрес сохранён, привязки и ссылки подписок сохранены.",
                         reply_markup=_keyboard([("Адрес панели", f"addr:{key}")]))


@router.message(XUIInput.bootstrap, F.text)
async def bootstrap_panel(message: Message, state: FSMContext) -> None:
    values = message.text.split()
    if len(values) != 4 or not values[2].isascii() or not values[2].isdigit() or len(values[2]) > 5:
        await message.answer("Укажите идентификатор, IPv4, SSH-порт и DNS-имя. Отмена — /cancel.")
        return
    key, host, port, domain = values
    # Receive the bot username before creating an invitation on the VPS.
    bot_name = (await runtime.get_bot().me()).username
    await state.clear()
    await message.answer("Проверяю VPS и запускаю установку. Это может занять несколько минут.")
    try:
        await xui_bootstrap.setup(key, host, int(port), domain)
        panel = runtime.panel_for(key)
        inbound = await add_reality_preset(panel, "auto", listen_port=24443)
        async with binding_lock, panel.config_lock:
            client = await create_named_client(panel, 0, config.plan_days, get_traffic_gb(), inbound["id"],
                                               username="client_" + uuid4().hex[:24])
            try:
                await xui_bootstrap.verify_subscription(client.sub_url, inbound["port"])
                token = db.create_client_invite(client.username, key)
            except Exception:
                await panel.delete_client(client.username)
                raise
    except PanelError as exc:
        markup = _keyboard([("Открыть панель", f"p:{key}:0")]) if key in runtime.xui_panels else None
        await message.answer(html.escape(str(exc)), reply_markup=markup)
        return
    await message.answer(f"Панель подключена, вход слушает порт {inbound['port']}.\n"
                         f"Одноразовое приглашение на 24 часа\n"
                         f"<code>https://t.me/{bot_name}?start=claim_{token}</code>",
                         reply_markup=_keyboard([("Открыть панель", f"p:{key}:0")]))
