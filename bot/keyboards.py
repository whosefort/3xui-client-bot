"""Инлайн-клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu(has_sub: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Моя подписка", callback_data="status")
    if has_sub:
        kb.button(text="🔁 Продлить", callback_data="renew")
    else:
        kb.button(text="🛒 Купить подписку", callback_data="buy")
    kb.button(text="💬 Связаться", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def confirm_paid(kind: str) -> InlineKeyboardMarkup:
    # kind: buy | renew
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data=f"paid:{kind}")
    kb.button(text="↩️ Назад", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_decision(req_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"adm:ok:{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm:no:{req_id}")
    kb.adjust(2)
    return kb.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ В меню", callback_data="menu")
    return kb.as_markup()
