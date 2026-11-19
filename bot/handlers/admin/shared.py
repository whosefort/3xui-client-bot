from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from ...config import config
from ...runtime import get_bot

log = logging.getLogger("admin")


router = Router()


router.message.filter(F.from_user.id.in_(config.admin_ids))


router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


class AdminFSM(StatesGroup):
    grant = State()
    broadcast_ids = State()      # ждём список получателей (для адресной рассылки)
    broadcast = State()          # ждём текст рассылки
    broadcast_confirm = State()  # текст получен, ждём подтверждения
    card_msg = State()           # ждём текст личного сообщения конкретному клиенту
    card_extend = State()        # ждём число месяцев для ручного продления
    card_subtract = State()      # ждём число месяцев для коррекции (убавить срок)
    card_bind = State()          # ждём tg_id для ручной привязки клиента
    set_price = State()
    set_req = State()
    set_traffic = State()        # ждём новый месячный лимит трафика (ГБ)
    add_server = State()         # ждём "IP [имя]" новой ноды
    card_label = State()         # ждём текст ручной подписи клиента
    card_note = State()          # ждём текст описания клиента (для админа)
    rename_server = State()      # ждём новое имя сервера (remark хоста)
    server_sni = State()         # ждём домен для персонального SNI одной ноды
    server_sni_scan = State()    # ждём цель скана для персонального SNI одной ноды
    reality_sni = State()        # ждём домен для смены SNI
    reality_scan = State()       # ждём цель (IP/CIDR/домен) для скана RealiTLScanner
    reality_port = State()       # ждём новый порт REALITY-инбаунда
    reality_spx = State()        # ждём новый SpiderX-путь
    warp_domain = State()        # ждём домен для ручной маршрутизации через WARP
    direct_domains = State()     # ждём свои домены для direct-списка Happ-профиля
    proxy_domains = State()      # ждём свои домены для proxy-списка Happ-профиля


def _add_server_available() -> bool:
    return config.panel_backend == "marzban" and config.node_provision_enabled


def _servers_available() -> bool:
    return config.panel_backend == "marzban"


async def _safe_edit(message: Message, text: str, reply_markup=None) -> None:
    """edit_text, но не роняет апдейт, если контент и клавиатура ничем не
    отличаются от текущих (например повторный клик по той же ноде без
    изменений с прошлого показа) — Telegram в этом случае отвечает 400
    'message is not modified', и без catch это всплывало необработанным
    исключением в логах (см. чат)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def _safe_user_msg(tg_id: int, text: str, reply_markup=None) -> None:
    try:
        await get_bot().send_message(tg_id, text, reply_markup=reply_markup)
    except Exception as e:  # noqa: BLE001
        log.warning("Не доставлено пользователю %s: %s", tg_id, e)
