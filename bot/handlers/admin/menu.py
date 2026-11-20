from __future__ import annotations

from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ...keyboards import admin_kb
from .shared import _add_server_available, _servers_available, router


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
