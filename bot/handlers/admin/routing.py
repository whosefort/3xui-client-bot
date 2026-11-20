from __future__ import annotations

import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ... import happ_routing
from ...keyboards import direct_domains_kb, proxy_domains_kb
from .shared import AdminFSM, _safe_edit, router


async def _add_domains(message: Message, state: FSMContext, add_domains, show_screen) -> None:
    await state.clear()
    good, bad = happ_routing.normalize(message.text or "")
    if not good:
        await message.answer("Не нашёл ни одного домена. Отменено.")
        return
    try:
        added, skipped = add_domains(good)
    except happ_routing.RoutingError as e:
        await message.answer(f"❌ Не удалось: {html.escape(str(e))}")
        return
    lines = []
    if added:
        lines.append("✅ Добавлено: " + ", ".join(f"<code>{html.escape(d)}</code>" for d in added))
    if skipped:
        lines.append("↪️ Уже были: " + ", ".join(f"<code>{html.escape(d)}</code>" for d in skipped))
    if bad:
        lines.append("⚠️ Не похоже на домен: " + ", ".join(html.escape(b) for b in bad))
    await message.answer("\n".join(lines))
    await show_screen(message, edit=False)


async def _remove_domain(cb: CallbackQuery, list_domains, remove_domain, show_screen) -> None:
    idx_s = cb.data.split(":", 2)[2]
    custom = list_domains()
    if not idx_s.isdigit() or int(idx_s) >= len(custom):
        await cb.answer("Список изменился, открой заново", show_alert=True)
        await show_screen(cb.message, edit=True)
        return
    domain = custom[int(idx_s)]
    try:
        remove_domain(domain)
    except happ_routing.RoutingError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    await cb.answer(f"Убрал {domain}")
    await show_screen(cb.message, edit=True)


def _direct_text(custom: list[str]) -> str:
    try:
        base = happ_routing.base_direct()
    except happ_routing.RoutingError as e:
        base = [f"(не прочитан: {e})"]
    mine = "\n".join(f"• <code>{html.escape(d)}</code>" for d in custom) or "— пока пусто"
    return (
        "↪️ <b>Direct-домены</b> — идут у клиента напрямую, мимо VPN "
        "(Happ routing-профиль, заголовок <code>routing</code> на sub.-домене).\n\n"
        f"<b>Свои ({len(custom)}):</b>\n{mine}\n\n"
        f"<b>База из репо ({len(base)}), отсюда не правится:</b>\n"
        f"<code>{html.escape(', '.join(base))}</code>\n\n"
        "Применяется сразу, без перезапусков. Клиент подхватит при следующем "
        "обновлении подписки в приложении. Домен покрывает и свои поддомены."
    )


async def _direct_screen(message: Message, edit: bool) -> None:
    custom = happ_routing.custom_domains()
    text, markup = _direct_text(custom), direct_domains_kb(custom)
    if edit:
        await _safe_edit(message, text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "rl:direct")
async def cb_rl_direct(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await _direct_screen(cb.message, edit=True)


@router.callback_query(F.data == "rl:directadd")
async def cb_rl_directadd(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.direct_domains)
    await cb.message.answer(
        "Пришли домены, которые пускать напрямую — можно несколько сразу, через "
        "пробел, запятую или с новой строки. Ссылку целиком тоже пойму:\n"
        "<code>zoom.us, https://kontur.ru/talk</code>\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.direct_domains, F.text)
async def direct_domains_input(message: Message, state: FSMContext) -> None:
    await _add_domains(message, state, happ_routing.add, _direct_screen)


@router.callback_query(F.data.startswith("rl:directrm:"))
async def cb_rl_directrm(cb: CallbackQuery) -> None:
    await _remove_domain(cb, happ_routing.custom_domains, happ_routing.remove, _direct_screen)


def _proxy_text(custom: list[str]) -> str:
    try:
        base = happ_routing.base_proxy()
    except happ_routing.RoutingError as e:
        base = [f"(не прочитан: {e})"]
    mine = "\n".join(f"• <code>{html.escape(d)}</code>" for d in custom) or "— пока пусто"
    return (
        "🔀 <b>Proxy-домены</b> — идут у клиента только через VPN "
        "(Happ routing-профиль, заголовок <code>routing</code> на sub.-домене). "
        "Нужны, когда сервис должен явно не ходить напрямую.\n\n"
        f"<b>Свои ({len(custom)}):</b>\n{mine}\n\n"
        f"<b>База из репо ({len(base)}), отсюда не правится:</b>\n"
        f"<code>{html.escape(', '.join(base))}</code>\n\n"
        "Применяется сразу, без перезапусков. Клиент подхватит при следующем "
        "обновлении подписки в приложении. Домен покрывает и свои поддомены."
    )


async def _proxy_screen(message: Message, edit: bool) -> None:
    custom = happ_routing.custom_proxy_domains()
    text, markup = _proxy_text(custom), proxy_domains_kb(custom)
    if edit:
        await _safe_edit(message, text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "rl:proxy")
async def cb_rl_proxy(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await _proxy_screen(cb.message, edit=True)


@router.callback_query(F.data == "rl:proxyadd")
async def cb_rl_proxyadd(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.proxy_domains)
    await cb.message.answer(
        "Пришли домены, которые пускать только через VPN — можно несколько "
        "сразу, через пробел, запятую или с новой строки. Ссылку целиком тоже "
        "пойму:\n<code>github.com, https://files-hub.com/download</code>\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.proxy_domains, F.text)
async def proxy_domains_input(message: Message, state: FSMContext) -> None:
    await _add_domains(message, state, happ_routing.add_proxy, _proxy_screen)


@router.callback_query(F.data.startswith("rl:proxyrm:"))
async def cb_rl_proxyrm(cb: CallbackQuery) -> None:
    await _remove_domain(cb, happ_routing.custom_proxy_domains, happ_routing.remove_proxy, _proxy_screen)
