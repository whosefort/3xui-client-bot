"""Конфигурация из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    # Терпим к мусору: выкидываем невидимые байты/BOM/replacement-символы
    # (артефакты heredoc/SSH) и любые нечисловые токены — бот не падает на старте.
    clean = "".join(c for c in raw if c.isdigit() or c in "-, ")
    out = []
    for tok in clean.replace(" ", "").split(","):
        if tok and tok.lstrip("-").isdigit():
            out.append(int(tok))
    return out


# Алиас для совместимости: ADMIN_IDS парсим тем же терпимым способом.
_ids = _int_list


def _safe_int(raw: str, default: int = 0) -> int:
    raw = "".join(c for c in (raw or "") if c.isdigit() or c == "-")
    try:
        return int(raw) if raw and raw != "-" else default
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "")
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


@dataclass
class Config:
    # Telegram
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=lambda: _ids(os.getenv("ADMIN_IDS", "")))
    support_chat_id: int = _safe_int(os.getenv("SUPPORT_CHAT_ID", "0"))

    # 3X-UI
    xui_base_url: str = os.getenv("XUI_BASE_URL", "").rstrip("/")
    xui_auth: str = os.getenv("XUI_AUTH", "token")  # token | login
    xui_api_token: str = os.getenv("XUI_API_TOKEN", "")
    xui_username: str = os.getenv("XUI_USERNAME", "")
    xui_password: str = os.getenv("XUI_PASSWORD", "")
    xui_2fa_secret: str = os.getenv("XUI_2FA_SECRET", "")

    # План подписки
    plan_days: int = _safe_int(os.getenv("PLAN_DAYS", "30"), 30)
    plan_traffic_gb: int = _safe_int(os.getenv("PLAN_TRAFFIC_GB", "150"), 150)
    default_inbound_ids: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("DEFAULT_INBOUND_IDS", "1"))
    )
    # flow нового клиента (на этой панели inbound vless с xtls-rprx-vision)
    client_flow: str = os.getenv("CLIENT_FLOW", "xtls-rprx-vision")
    # Шаблон ссылки-подписки с плейсхолдером {subId}. Если пусто — base/sub/{subId}.
    sub_url_template: str = os.getenv("SUB_URL_TEMPLATE", "")

    # Прочее
    default_price: str = os.getenv("DEFAULT_PRICE", "199")
    default_requisites: str = os.getenv("DEFAULT_REQUISITES", "")
    db_path: str = os.getenv("DB_PATH", "data/bot.db")
    remind_days_before: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("REMIND_DAYS_BEFORE", "3,1,0"))
    )
    remind_hour: int = _safe_int(os.getenv("REMIND_HOUR", "11"), 11)

    # Бэкап в Cloudflare R2 (опционально; выключается флагом или паузой из админки)
    backup_enabled: bool = _env_bool("BACKUP_ENABLED", False)
    r2_endpoint: str = os.getenv("R2_ENDPOINT", "")
    r2_bucket: str = os.getenv("R2_BUCKET", "")
    r2_access_key_id: str = os.getenv("R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    backup_age_pubkey: str = os.getenv("BACKUP_AGE_PUBKEY", "")  # публичный ключ, НЕ секрет

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def validate(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.admin_ids:
            missing.append("ADMIN_IDS")
        if not self.xui_base_url:
            missing.append("XUI_BASE_URL")
        if self.xui_auth == "token" and not self.xui_api_token:
            missing.append("XUI_API_TOKEN")
        if self.xui_auth == "login" and not (self.xui_username and self.xui_password):
            missing.append("XUI_USERNAME/XUI_PASSWORD")
        if missing:
            raise RuntimeError(f"Не заданы обязательные переменные .env: {', '.join(missing)}")

        # Бэкап опционален — не валим старт, но предупреждаем о неполной настройке.
        if self.backup_enabled and not (self.r2_endpoint and self.r2_bucket
                                        and self.r2_access_key_id and self.r2_secret_access_key):
            import logging
            logging.getLogger("config").warning(
                "BACKUP_ENABLED=true, но R2 настроен не полностью — бэкап работать не будет")


config = Config()
