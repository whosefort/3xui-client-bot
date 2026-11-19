from __future__ import annotations

import html

from aiogram.filters import Command
from aiogram.types import Message

from ... import db
from ...config import config
from .broadcast import _do_broadcast
from .clients import _do_grant, _parse_tgid
from .shared import router


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
    from ... import backup
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
