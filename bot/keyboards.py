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
    kb.button(text="❓ Частые вопросы", callback_data="faq")
    kb.button(text="💬 Связаться", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def faq_kb(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """items: [(id, заголовок), ...] — оглавление FAQ."""
    kb = InlineKeyboardBuilder()
    for item_id, title in items:
        kb.button(text=title, callback_data=f"faq:{item_id}")
    kb.button(text="↩️ В меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def faq_answer_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К вопросам", callback_data="faq")
    kb.button(text="💬 Связаться", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def reminder_kb() -> InlineKeyboardMarkup:
    """Текст reminder() говорит «нажмите Продлить» — у сообщения обязана
    быть эта кнопка, иначе человеку самому искать меню заново."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Продлить", callback_data="renew")
    kb.adjust(1)
    return kb.as_markup()


def status_kb() -> InlineKeyboardMarkup:
    """Клавиатура под экраном статуса подписки (активная/бессрочная) —
    добавляет troubleshooting-кнопку для не подгружающейся ссылки-подписки."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Продлить", callback_data="renew")
    kb.button(text="📲 Как настроить", callback_data="setup:1")
    kb.button(text="⚠️ Подписка не подгружается?", callback_data="sub_fallback")
    kb.button(text="💬 Связаться", callback_data="support")
    kb.button(text="↩️ В меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


# ===================== Мастер настройки (3 шага) =====================

_HAPP_APPSTORE = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
_HAPP_GOOGLE_PLAY = "https://play.google.com/store/apps/details?id=com.happproxy"


def setup_start_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📲 Настроить (3 шага)", callback_data="setup:1")
    kb.button(text="💬 Связаться", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def bind_ok_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📲 Настроить (3 шага)", callback_data="setup:1")
    kb.button(text="📊 Моя подписка", callback_data="status")
    kb.adjust(1)
    return kb.as_markup()


def setup_step1_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🍏 App Store", url=_HAPP_APPSTORE)
    kb.button(text="🤖 Google Play", url=_HAPP_GOOGLE_PLAY)
    kb.button(text="🍏 Пропал из App Store?", callback_data="faq:happ_removed")
    kb.button(text="✅ Установил, дальше", callback_data="setup:2")
    kb.button(text="↩️ В меню", callback_data="menu")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


def setup_step2_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Добавил, дальше", callback_data="setup:3")
    kb.button(text="⬅️ Назад", callback_data="setup:1")
    kb.adjust(1)
    return kb.as_markup()


def setup_step3_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Включил, готово", callback_data="setup:done")
    kb.button(text="🤔 Не нашёл эту настройку", callback_data="support")
    kb.button(text="⬅️ Назад", callback_data="setup:2")
    kb.adjust(1)
    return kb.as_markup()


def setup_done_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Моя подписка", callback_data="status")
    kb.button(text="🚫 VPN не подключается", callback_data="faq:vpn_down")
    kb.button(text="↩️ В меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def sub_fallback_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к подписке", callback_data="status")
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
ADM_ADD_SERVER = "🖥 Добавить сервер"
ADM_SERVERS = "🌍 Серверы"
ADM_REALITY = "🛡 Соединение"


def admin_kb(show_add_server: bool = False, show_servers: bool = False,
             show_reality: bool = False) -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода у админа."""
    kb = ReplyKeyboardBuilder()
    kb.button(text=ADM_REQUESTS)
    kb.button(text=ADM_EXPIRING)
    kb.button(text=ADM_CLIENTS)
    kb.button(text=ADM_GRANT)
    kb.button(text=ADM_SETTINGS)
    kb.button(text=ADM_BROADCAST)
    rows = [2, 2, 2]
    tail = int(show_servers) + int(show_add_server) + int(show_reality)
    if show_servers:
        kb.button(text=ADM_SERVERS)
    if show_reality:
        kb.button(text=ADM_REALITY)
    if show_add_server:
        kb.button(text=ADM_ADD_SERVER)
    if tail:
        rows.append(tail)
    kb.adjust(*rows)
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
    kb.button(text="✏️ Подпись", callback_data=f"cli:label:{email}")
    kb.button(text="📝 Описание", callback_data=f"cli:note:{email}")
    kb.button(text="♾ Без ограничений", callback_data=f"cli:unlim:{email}")
    kb.button(text="🗑 Удалить", callback_data=f"cli:del:{email}")
    kb.button(text="↩️ К списку", callback_data="cli:list")
    kb.adjust(2, 2, 2, 2, 1, 1, 1)
    return kb.as_markup()


def servers_list_kb(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """items: [(server_key, подпись-кнопки), ...] — server_key = 'tag:index'."""
    kb = InlineKeyboardBuilder()
    for key, label in items:
        kb.button(text=label, callback_data=f"srv:open:{key}")
    kb.button(text="🔄 Обновить xray на всех нодах", callback_data="srv:xrayup")
    kb.button(text="✖️ Закрыть", callback_data="srv:close")
    kb.adjust(1)
    return kb.as_markup()


def xray_upgrade_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Обновить все ноды", callback_data="srv:xrayupgo")
    kb.button(text="✖️ Отмена", callback_data="srv:list")
    kb.adjust(1)
    return kb.as_markup()


def server_sni_picker_kb(domains: list[str], show_scan: bool = True) -> InlineKeyboardMarkup:
    """domains: уже без дублей, в порядке показа — сперва то, что реально
    используется в кластере, потом затравка из reality_admin.SUGGESTED_DOMAINS."""
    kb = InlineKeyboardBuilder()
    for d in domains:
        cb = f"srv:snipick:{d}"
        if len(cb.encode()) <= 64:
            kb.button(text=d, callback_data=cb)
    if show_scan:
        kb.button(text="🔍 Просканировать", callback_data="srv:sniscan")
    kb.button(text="✏️ Свой домен", callback_data="srv:snimanual")
    kb.button(text="✖️ Отмена", callback_data="srv:list")
    kb.adjust(1)
    return kb.as_markup()


def server_card_kb(key: str, fragment_on: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Переименовать", callback_data=f"srv:rename:{key}")
    kb.button(text="🎭 Свой SNI для этой ноды", callback_data=f"srv:sni:{key}")
    kb.button(text="♻️ Сбросить SNI (общий)", callback_data=f"srv:snireset:{key}")
    kb.button(
        text=("🧩 Fragment: выключить" if fragment_on else "🧩 Fragment: включить"),
        callback_data=f"srv:fragtoggle:{key}",
    )
    kb.button(text="🫆 TLS-фингерпринт", callback_data=f"srv:fp:{key}")
    kb.button(text="↩️ К списку", callback_data="srv:list")
    kb.adjust(1)
    return kb.as_markup()


def server_fp_picker_kb(key: str, options: list[str], current: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for fp in options:
        mark = "✅ " if fp == current else ""
        label = f"{mark}{fp}" + (" (рекомендуется)" if fp == "randomized" else "")
        kb.button(text=label, callback_data=f"srv:fppick:{fp}:{key}")
    kb.button(text="✖️ Отмена", callback_data=f"srv:open:{key}")
    kb.adjust(1)
    return kb.as_markup()


def reality_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Сменить SNI (домен)", callback_data="rl:sni")
    kb.button(text="🔍 Найти SNI-домены (скан)", callback_data="rl:scan")
    kb.button(text="🔑 Перегенерить ключи", callback_data="rl:keys")
    kb.button(text="🆔 Перегенерить shortId", callback_data="rl:shortids")
    kb.button(text="✖️ Закрыть", callback_data="rl:close")
    kb.adjust(1)
    return kb.as_markup()


def reality_confirm_kb(action: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"rl:go:{action}")
    kb.button(text="✖️ Отмена", callback_data="rl:menu")
    kb.adjust(1)
    return kb.as_markup()


def reality_sni_result_kb(ok: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if ok:
        kb.button(text="✅ Применить", callback_data="rl:sniapply")
    else:
        kb.button(text="⚠️ Всё равно применить", callback_data="rl:sniapply")
    kb.button(text="✖️ Отмена", callback_data="rl:menu")
    kb.adjust(1)
    return kb.as_markup()


def reality_scan_results_kb(domains: list[str], show_scan: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in domains:
        cb = f"rl:pick:{d}"
        if len(cb.encode()) <= 64:
            kb.button(text=d, callback_data=cb)
    if show_scan:
        kb.button(text="🔍 Просканировать ещё", callback_data="rl:scan")
    kb.button(text="✏️ Свой домен", callback_data="rl:snimanual")
    kb.button(text="✖️ Отмена", callback_data="rl:menu")
    kb.adjust(1)
    return kb.as_markup()


def confirm_delete_kb(email: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"cli:delok:{email}")
    kb.button(text="↩️ Отмена", callback_data=f"cli:open:{email}")
    kb.adjust(1)
    return kb.as_markup()


def confirm_unlimited_kb(email: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="♾ Да, без ограничений", callback_data=f"cli:unlimok:{email}")
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
