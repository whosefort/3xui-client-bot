"""Инлайн- и reply-клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def main_menu(has_sub: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Моя подписка", callback_data="status")
    if has_sub:
        kb.button(text="🔁 Продлить", callback_data="renew")
    else:
        kb.button(text="🛒 Купить подписку", callback_data="buy")
        kb.button(text="🔑 У меня уже есть подписка", callback_data="have_sub")
    kb.button(text="💬 Связаться", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def confirm_paid(kind: str) -> InlineKeyboardMarkup:
    # kind: buy | renew
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data=f"paid:{kind}")
    if kind == "buy":
        # для нового клиента: вдруг ему уже выдали подписку вручную
        kb.button(text="🔑 У меня уже есть подписка", callback_data="have_sub")
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


# ======================= АДМИНКА =======================

# Подписи кнопок reply-клавиатуры (по ним же ловим нажатия в хендлерах).
ADM_REQUESTS = "📋 Заявки"
ADM_EXPIRING = "⏳ Истекают"
ADM_CLIENTS = "👥 Клиенты"
ADM_GRANT = "➕ Выдать вручную"
ADM_SETTINGS = "💳 Цена/реквизиты"
ADM_BROADCAST = "📢 Рассылка"


def admin_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода у админа."""
    kb = ReplyKeyboardBuilder()
    kb.button(text=ADM_REQUESTS)
    kb.button(text=ADM_EXPIRING)
    kb.button(text=ADM_CLIENTS)
    kb.button(text=ADM_GRANT)
    kb.button(text=ADM_SETTINGS)
    kb.button(text=ADM_BROADCAST)
    kb.adjust(2, 2, 2)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def clients_list_kb(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """items: [(email, подпись-кнопки), ...] — каждая открывает карточку клиента."""
    kb = InlineKeyboardBuilder()
    for email, label in items:
        cb = f"cli:open:{email}"
        if len(cb.encode()) <= 64:           # лимит Telegram на callback_data
            kb.button(text=label, callback_data=cb)
    kb.button(text="✖️ Закрыть", callback_data="cli:close")
    kb.adjust(1)
    return kb.as_markup()


def client_card_kb(email: str, enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Продлить 30д", callback_data=f"cli:ext:{email}")      # месяц: сброс+лимит
    kb.button(text="📅 Продлить N мес", callback_data=f"cli:extn:{email}")   # N мес: N×лимит+сброс
    kb.button(text="➖ Убавить N мес", callback_data=f"cli:sub:{email}")     # коррекция срока
    kb.button(text=("⛔️ Выключить" if enabled else "✅ Включить"),
              callback_data=f"cli:tog:{email}")
    kb.button(text="🔗 Ссылка", callback_data=f"cli:lnk:{email}")
    kb.button(text="✉️ Написать", callback_data=f"cli:msg:{email}")
    kb.button(text="🆔 Привязать tg_id", callback_data=f"cli:bind:{email}")
    kb.button(text="🗑 Удалить", callback_data=f"cli:del:{email}")
    kb.button(text="↩️ К списку", callback_data="cli:list")
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def confirm_delete_kb(email: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"cli:delok:{email}")
    kb.button(text="↩️ Отмена", callback_data=f"cli:open:{email}")
    kb.adjust(1)
    return kb.as_markup()


def settings_kb(backup_btn: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить цену", callback_data="set:price")
    kb.button(text="✏️ Изменить реквизиты", callback_data="set:req")
    kb.button(text="✏️ Лимит трафика (ГБ/мес)", callback_data="set:traffic")
    if backup_btn:                       # показываем только если бэкап включён в .env
        kb.button(text=backup_btn, callback_data="bk:toggle")
        kb.button(text="💾 Сделать бэкап сейчас", callback_data="bk:now")
    kb.adjust(1)
    return kb.as_markup()


def broadcast_target_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Всем клиентам", callback_data="bc:all")
    kb.button(text="🎯 Выбранным (по списку)", callback_data="bc:some")
    kb.adjust(1)
    return kb.as_markup()


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data="bc:send")
    kb.button(text="✖️ Отмена", callback_data="bc:cancel")
    kb.adjust(1)
    return kb.as_markup()
