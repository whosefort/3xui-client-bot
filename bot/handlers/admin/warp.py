from __future__ import annotations

import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ... import node_provision, warp, warp_admin
from ...keyboards import (
    warp_domadd_categories_kb,
    warp_domadd_domains_kb,
    warp_node_kb,
    warp_nodes_kb,
)
from .shared import AdminFSM, _safe_edit, log, router


def _warp_tag(address: str) -> str:
    """Тег outbound'а детерминированно выводится из IP — не нужна отдельная
    таблица «нода -> тег», просто пересчитывается каждый раз."""
    return "warp-" + address.replace(".", "-").replace(":", "-")


async def _warp_nodes_screen(cb: CallbackQuery) -> None:
    try:
        nodes = await node_provision.list_nodes()
        outbounds = await warp_admin.list_warp_outbounds()
    except (node_provision.ProvisionError, warp_admin.WarpAdminError) as e:
        await _safe_edit(cb.message, f"❌ Не удалось получить данные: {e}")
        return
    tags = {o["tag"] for o in outbounds}
    items = [(n["address"], n.get("name") or n["address"], _warp_tag(n["address"]) in tags) for n in nodes]
    if not items:
        await _safe_edit(cb.message, "Нод нет.")
        return
    await _safe_edit(
        cb.message,
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
        await _safe_edit(cb.message, f"❌ Не удалось получить данные: {e}")
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
    await _safe_edit(
        cb.message,
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
    await _safe_edit(
        cb.message,
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
    await _safe_edit(
        cb.message,
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
