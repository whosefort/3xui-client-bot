from __future__ import annotations

import asyncio
import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ... import db
from ... import keyboards as kb
from ...keyboards import broadcast_confirm_kb, broadcast_target_kb
from ...runtime import get_bot
from .shared import AdminFSM, router


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
