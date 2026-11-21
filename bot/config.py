"""Конфигурация из переменных окружения (.env)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()


def _json(key: str, default=None):
    raw = os.getenv(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


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

    # Выбор бэкенда панели: xui | marzban
    panel_backend: str = os.getenv("PANEL_BACKEND", "xui").strip().lower()
    # Несколько панелей разом: PANELS=marzban,xui (первая — главная). Пусто —
    # одна панель из PANEL_BACKEND. panel_backend ниже всегда = главная.
    panels: list = field(default_factory=lambda: [
        k.strip().lower() for k in os.getenv("PANELS", "").split(",") if k.strip()
    ])

    # Marzban
    marzban_url: str = os.getenv("MARZBAN_URL", "").rstrip("/")
    marzban_username: str = os.getenv("MARZBAN_USERNAME", "")
    marzban_password: str = os.getenv("MARZBAN_PASSWORD", "")
    marzban_reset_strategy: str = os.getenv("MARZBAN_RESET_STRATEGY", "month")
    # Опционально: чем создавать юзера. Пусто → берём ВСЕ инбаунды панели.
    marzban_proxies: Optional[dict] = field(default_factory=lambda: _json("MARZBAN_PROXIES", None))
    marzban_inbounds: Optional[dict] = field(default_factory=lambda: _json("MARZBAN_INBOUNDS", None))
    private_marzban_url: str = os.getenv("PRIVATE_MARZBAN_URL", "").rstrip("/")
    private_marzban_username: str = os.getenv("PRIVATE_MARZBAN_USERNAME", "")
    private_marzban_password: str = os.getenv("PRIVATE_MARZBAN_PASSWORD", "")
    private_marzban_inbound: str = os.getenv("PRIVATE_MARZBAN_INBOUND", "").strip()

    # Авторазвёртывание нод («Добавить сервер»): локальный HTTP-порт, по которому
    # bootstrap_token.sh забирает cert новой ноды по одноразовому токену.
    # Не 443/80 — те уже заняты панелью. Порт из списка, который Cloudflare
    # проксирует для любого домена без доп. настройки (443/2053/2083/2087/2096/8443).
    node_provision_enabled: bool = _env_bool("NODE_PROVISION_ENABLED", False)
    node_provision_port: int = _safe_int(os.getenv("NODE_PROVISION_PORT", "8443"), 8443)
    node_token_ttl_seconds: int = _safe_int(os.getenv("NODE_TOKEN_TTL_SECONDS", "1800"), 1800)

    # 3X-UI
    xui_base_url: str = os.getenv("XUI_BASE_URL", "").rstrip("/")
    xui_auth: str = os.getenv("XUI_AUTH", "token")  # token | login
    xui_api_token: str = os.getenv("XUI_API_TOKEN", "")
    xui_username: str = os.getenv("XUI_USERNAME", "")
    xui_password: str = os.getenv("XUI_PASSWORD", "")
    xui_2fa_secret: str = os.getenv("XUI_2FA_SECRET", "")

    xui_managed_panels: str = field(default_factory=lambda: os.getenv("XUI_MANAGED_PANELS", ""))

    def managed_xui(self) -> dict[str, tuple[str, str, str, str, str, str]]:
        """Return panel id → (name, HTTPS URL, bearer token, exit client, subscription URL template)."""
        if not self.xui_managed_panels.strip():
            return {}
        try:
            rows = json.loads(self.xui_managed_panels)
        except ValueError:
            raise RuntimeError("XUI_MANAGED_PANELS должен быть JSON-объектом.") from None
        if not isinstance(rows, dict):
            raise RuntimeError("XUI_MANAGED_PANELS должен быть JSON-объектом.")
        panels = {}
        for key, row in rows.items():
            if not re.fullmatch(r"[a-z0-9_-]{1,12}", key) or not isinstance(row, dict):
                raise RuntimeError("Неверный идентификатор или параметры панели 3x-ui.")
            name, base, token_env = (row.get(k) for k in ("name", "url", "token_env"))
            if not all(isinstance(v, str) and v.strip() for v in (name, base, token_env)):
                raise RuntimeError(f"Для панели {key} нужны name, url и token_env.")
            try:
                url = urlsplit(base)
                valid_port = url.port is None or 0 < url.port <= 65535
            except ValueError:
                valid_port = False
            if (not valid_port or url.scheme != "https" or not url.hostname
                    or url.username is not None or url.query or url.fragment
                    or len(name) > 60 or not re.fullmatch(r"XUI_[A-Z0-9_]+_TOKEN", token_env)):
                raise RuntimeError(f"Некорректные параметры панели {key}.")
            token = os.getenv(token_env, "").strip()
            if not token:
                raise RuntimeError(f"Не задан {token_env} для панели {key}.")
            exit_client = row.get("exit_client", "")
            if not isinstance(exit_client, str) or len(exit_client) > 128:
                raise RuntimeError(f"Некорректный exit_client для панели {key}.")
            subscription = row.get("subscription_url", "")
            if not isinstance(subscription, str):
                raise RuntimeError(f"Некорректный subscription_url для панели {key}.")
            if subscription:
                try:
                    sub = urlsplit(subscription)
                    sub_port = sub.port or 443
                except ValueError:
                    raise RuntimeError(f"Некорректный subscription_url для панели {key}.") from None
                if (not 0 < sub_port <= 65535 or sub.scheme != "https" or not sub.hostname or sub.username is not None
                        or sub.query or sub.fragment or not sub.path.endswith("/{subId}")
                        or subscription.count("{subId}") != 1):
                    raise RuntimeError(f"subscription_url панели {key} должен оканчиваться на /{{subId}}.")
            node_token_env = row.get("node_token_env", "")
            if not isinstance(node_token_env, str) or (
                node_token_env and not re.fullmatch(r"XUI_[A-Z0-9_]+_TOKEN", node_token_env)
            ):
                raise RuntimeError(f"Некорректный node_token_env для панели {key}.")
            node_token = os.getenv(node_token_env, "").strip() if node_token_env else ""
            if node_token_env and (not node_token or node_token == token):
                raise RuntimeError(f"Панели {key} нужен отдельный токен для связи нод.")
            panels[key] = (name, base.rstrip("/"), token, exit_client, subscription, node_token)
        if len({url for _, url, _, _, _, _ in panels.values()}) != len(panels):
            raise RuntimeError("Адреса панелей в XUI_MANAGED_PANELS должны быть уникальны.")
        urls = [urlsplit(sub) for _, _, _, _, sub, _ in panels.values() if sub]
        routes = {(url.hostname, url.port or 443, url.path) for url in urls}
        if len(routes) != len(urls):
            raise RuntimeError("Шаблоны подписки именованных панелей должны быть уникальны.")
        return panels

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

    def __post_init__(self) -> None:
        if not self.panels:
            self.panels = [self.panel_backend]
        self.panel_backend = self.panels[0]

    def uses(self, kind: str) -> bool:
        return kind in self.panels

    def validate(self) -> None:
        missing = []
        bad = [k for k in self.panels if k not in ("xui", "marzban")]
        if bad:
            missing.append(f"PANELS: неизвестные {bad} (можно xui, marzban)")
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.admin_ids:
            missing.append("ADMIN_IDS")
        if self.uses("marzban"):
            if not self.marzban_url:
                missing.append("MARZBAN_URL")
            if not self.marzban_username:
                missing.append("MARZBAN_USERNAME")
            if not self.marzban_password:
                missing.append("MARZBAN_PASSWORD")
        if self.uses("xui"):
            if not self.xui_base_url:
                missing.append("XUI_BASE_URL")
            if self.xui_auth == "token" and not self.xui_api_token:
                missing.append("XUI_API_TOKEN")
            if self.xui_auth == "login" and not (self.xui_username and self.xui_password):
                missing.append("XUI_USERNAME/XUI_PASSWORD")
        if missing:
            raise RuntimeError(f"Не заданы обязательные переменные .env: {', '.join(missing)}")

        if self.private_marzban_url:
            url = urlsplit(self.private_marzban_url)
            shared = urlsplit(self.marzban_url)
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or url.query or url.fragment or url.hostname == shared.hostname):
                raise RuntimeError("PRIVATE_MARZBAN_URL должен указывать на отдельный HTTPS-хост.")
            if not (self.private_marzban_username and self.private_marzban_password
                    and self.private_marzban_inbound):
                raise RuntimeError("Для отдельной панели нужны PRIVATE_MARZBAN_USERNAME, "
                                   "PRIVATE_MARZBAN_PASSWORD и PRIVATE_MARZBAN_INBOUND.")

        self.managed_xui()

        # Бэкап опционален — не валим старт, но предупреждаем о неполной настройке.
        if self.backup_enabled and not (self.r2_endpoint and self.r2_bucket
                                        and self.r2_access_key_id and self.r2_secret_access_key):
            import logging
            logging.getLogger("config").warning(
                "BACKUP_ENABLED=true, но R2 настроен не полностью — бэкап работать не будет")


config = Config()
