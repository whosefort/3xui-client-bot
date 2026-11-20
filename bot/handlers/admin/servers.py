from __future__ import annotations

import asyncio
import html
import ipaddress
import subprocess

import aiohttp
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ... import db, node_provision, reality_admin, reality_scan, ssh_ops
from ... import keyboards as kb
from ...config import config
from ...keyboards import (
    server_card_kb,
    server_fp_picker_kb,
    server_sni_picker_kb,
    server_xray_channel_kb,
    server_xray_confirm_kb,
    xray_channel_pick_kb,
    xray_upgrade_confirm_kb,
)
from .shared import AdminFSM, _add_server_available, _servers_available, log, router


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
        "xray-core — пин из node/XRAY_VERSION (может быть pre-release). "
        "Ключевые слова (в любом месте, через пробел):\n"
        "• <code>latest</code> — образ marzban-node :latest\n"
        "• <code>xraystable</code> — xray-core последняя СТАБИЛЬНАЯ (не pre-release) "
        "с GitHub — шире совместимость со старыми клиентами (TV-приложения и т.п.)\n"
        "• <code>xraylatest</code> — xray-core последний тег вообще, включая pre-release\n"
        "<code>203.0.113.10 eu-frankfurt-02 xraystable</code>\n\n"
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
        elif low == "xraystable":
            xray_channel = "laststable"
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
    if xray_channel == "latest":
        xray_note = "xray-core: последний тег с GitHub, включая pre-release (выбрано вручную)"
    elif xray_channel == "laststable":
        xray_note = "xray-core: последняя СТАБИЛЬНАЯ с GitHub, без pre-release (выбрано вручную)"
    else:
        xray_note = "xray-core: пин из node/XRAY_VERSION"
    await message.answer(
        f"✅ Нода id={reg['node_id']} зарегистрирована в панели ({address}).\n"
        f"Токен живёт {ttl_min} мин, одноразовый. {image_note}, {xray_note}.\n\n"
        f"Вставь эту команду в SSH-сессию нового VPS (под root):\n\n"
        f"<code>{html.escape(cmd)}</code>"
    )


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


@router.callback_query(F.data.startswith("srv:nodexray:"))
async def cb_srv_nodexray(cb: CallbackQuery) -> None:
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
    try:
        pin = _xray_pin()
    except OSError:
        await cb.answer("node/XRAY_VERSION не найден", show_alert=True)
        return
    await cb.message.edit_text(
        f"🔄 Обновить xray-core на <b>{html.escape(_server_label(match))}</b> — "
        f"только эту ноду, остальные не тронутся. Ядро перезапустится, клиенты "
        f"на ней разорвут соединение на пару секунд.\n\n"
        f"Наш пин может быть pre-release. Последняя стабильная — без pre-release, "
        f"шире совместимость со старыми клиентами.",
        reply_markup=server_xray_channel_kb(key, pin),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv:nodexrayver:"))
async def cb_srv_nodexrayver(cb: CallbackQuery) -> None:
    rest = cb.data[len("srv:nodexrayver:"):]
    channel, _, key = rest.partition(":")
    if channel == "stable":
        try:
            version = _xray_pin()
        except OSError:
            await cb.answer("node/XRAY_VERSION не найден", show_alert=True)
            return
    elif channel == "laststable":
        await cb.answer("Спрашиваю GitHub (stable)…")
        version = await _github_latest_stable_xray_version()
        if not version:
            await cb.message.answer("❌ Не смог получить последнюю стабильную версию с GitHub. Попробуй позже.")
            return
    else:
        await cb.answer("Спрашиваю GitHub…")
        version = await _github_latest_xray_version()
        if not version:
            await cb.message.answer("❌ Не смог получить последнюю версию с GitHub. Попробуй ещё раз позже.")
            return
    await cb.message.edit_text(
        f"Обновить xray-core до <code>{html.escape(version)}</code> на этой ноде?",
        reply_markup=server_xray_confirm_kb(key, version),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv:nodexrayupgo:"))
async def cb_srv_nodexrayupgo(cb: CallbackQuery) -> None:
    rest = cb.data[len("srv:nodexrayupgo:"):]
    version, _, key = rest.partition(":")
    tag, _, idx = key.partition(":")
    await cb.answer()
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError as e:
        await cb.message.answer(f"❌ Не удалось получить список нод: {e}")
        return
    if not match:
        await cb.message.answer("Сервер не найден (список изменился)")
        return
    await cb.message.answer(f"Обновляю {html.escape(_server_label(match))} до {html.escape(version)}…")
    try:
        await ssh_ops.upgrade_node(match["address"], version)
    except ssh_ops.SSHOpError as e:
        await cb.message.answer(f"❌ {html.escape(_server_label(match))}: {html.escape(str(e))}")
        return
    except Exception:  # noqa: BLE001
        log.exception("node xray upgrade failed for %s", match["address"])
        await cb.message.answer("❌ Ошибка. См. логи.")
        return
    await cb.message.answer(f"✅ {html.escape(_server_label(match))} обновлена до {html.escape(version)}.")
    try:
        match = await _find_server(tag, idx)
    except node_provision.ProvisionError:
        return
    if match:
        await cb.message.answer(
            _server_card_text(match, tag),
            reply_markup=server_card_kb(key, fragment_on=match["fragment"]),
        )


def _xray_pin() -> str:
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "node", "XRAY_VERSION")
    with open(os.path.normpath(path)) as f:
        return f.read().strip()


async def _github_latest_xray_version() -> str | None:
    """Живой запрос к GitHub API (последний тег ВООБЩЕ, включая pre-release) —
    не полагаемся на node/XRAY_VERSION, тот пин обновляется руками и
    xray-core выходит часто (см. контекст в чате: залипание на старом пине
    уже путали с сетевыми проблемами)."""
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


async def _github_latest_stable_xray_version() -> str | None:
    """/releases/latest у GitHub сам отфильтровывает pre-release — в отличие
    от простого 'первый тег в списке', который сейчас почти всегда
    pre-release (XTLS месяцами не режет полноценных stable-релизов). Нужно
    для клиентов вроде TV-приложений, которые не следят за pre-release и
    ломаются на version skew, если сервер уехал слишком далеко вперёд —
    см. обсуждение в чате про 'socks error event .../removing connection'."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.github.com/repos/XTLS/Xray-core/releases/latest",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json(content_type=None)
                if r.status >= 400 or not data:
                    return None
                return data.get("tag_name")
    except Exception:  # noqa: BLE001
        log.exception("не смог получить latest STABLE xray-core с GitHub")
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
        "Наш пин — то, что сейчас в node/XRAY_VERSION (может быть pre-release). "
        "Последняя стабильная — GitHub /releases/latest, без pre-release, "
        "с ней совместимость шире (старые TV-приложения и т.п.). Последняя с "
        "GitHub — самый свежий тег вообще, включая pre-release.",
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
    elif channel == "laststable":
        await cb.answer("Спрашиваю GitHub (stable)…")
        version = await _github_latest_stable_xray_version()
        if not version:
            await cb.message.answer("❌ Не смог получить последнюю стабильную версию с GitHub. Попробуй позже.")
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
