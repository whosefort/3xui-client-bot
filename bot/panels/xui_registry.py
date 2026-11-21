"""Persist named 3x-ui endpoints without storing API credentials in SQLite."""
from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .. import db, runtime
from ..config import config
from .base import PanelError
from .xui import XUIClient

_setting = "xui_panel_registry"
_lock = asyncio.Lock()


def _records() -> dict:
    return json.loads(db.get_setting(_setting, "{}"))


def _url(value: str, *, subscription: bool = False) -> str:
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname and url.username is None
                 and not url.query and not url.fragment and (url.port is None or url.port > 0)
                 and not any(c.isspace() for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise PanelError("Укажите HTTPS-адрес без логина, параметров или фрагмента.")
    if subscription and (not url.path.endswith("/{subId}") or value.count("{subId}") != 1):
        raise PanelError("Адрес подписки должен оканчиваться на /{subId}.")
    return value.rstrip("/")


def _token(name: str) -> str:
    if not re.fullmatch(r"XUI_[A-Z0-9_]+_TOKEN", name):
        raise PanelError("Укажите имя переменной XUI_…_TOKEN.")
    value = os.getenv(name, "").strip()
    if not value:
        raise PanelError("Переменная с токеном отсутствует в окружении бота.")
    return value


def registry_cipher() -> Fernet:
    try:
        return Fernet(os.environ.get("XUI_REGISTRY_KEY", "").encode())
    except ValueError:
        raise PanelError("Настройте XUI_REGISTRY_KEY для автосетапа.") from None


def load(panels: dict[str, tuple[str, XUIClient]]) -> None:
    for key, row in _records().items():
        if "token_env" in row or "token_enc" in row:
            if key in panels:
                raise RuntimeError(f"Идентификатор 3x-ui {key} занят в двух источниках.")
            if "token_enc" in row:
                try:
                    token = registry_cipher().decrypt(row["token_enc"].encode()).decode()
                except InvalidToken:
                    raise RuntimeError("Не удалось расшифровать токен панели 3x-ui.") from None
            else:
                token = os.getenv(row["token_env"], "").strip()
            panels[key] = (row["name"], XUIClient(row["url"], api_token=token,
                                                sub_url_template=row["subscription"]))
        elif key in panels:
            panels[key][1].base = row["url"]


def validate_key(key: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,15}", key) or key in {"xui", "xui-default"}:
        raise PanelError("Идентификатор должен начинаться с латинской буквы и содержать до 16 символов a–z, 0–9, дефис.")


async def register(key: str, name: str, url: str, subscription: str, token_env: str = "",
                   *, generated_token: str = "") -> None:
    validate_key(key)
    if not name or len(name) > 60:
        raise PanelError("Название должно содержать от 1 до 60 символов.")
    url, subscription = _url(url), _url(subscription, subscription=True)
    token = generated_token or _token(token_env)
    secret = ({"token_enc": registry_cipher().encrypt(token.encode()).decode()} if generated_token
              else {"token_env": token_env})
    async with _lock:
        records = _records()
        if key in runtime.xui_panels or key in records:
            raise PanelError("Идентификатор уже занят.")
        from .ownership import share_route
        if url == config.xui_base_url or any(p.base == url or (p.sub_url_template and share_route(p.sub_url_template) == share_route(subscription))
               for _, p in runtime.xui_panels.values()):
            raise PanelError("Адрес панели или маршрут подписки уже используется.")
        panel = XUIClient(url, api_token=token, sub_url_template=subscription)
        try:
            await panel.server_status()
            await panel.list_inbounds()
            await panel.list_clients(force=True)
            records[key] = dict(name=name, url=url, subscription=subscription, **secret)
            db.set_setting(_setting, json.dumps(records))
        except BaseException:
            await panel.close()
            raise
        runtime.xui_panels[key] = name, panel


async def change_address(key: str, url: str) -> None:
    url = _url(url)
    async with _lock:
        panel = runtime.panel_for(key)
        if config.uses("xui") and panel.base == config.xui_base_url:
            raise PanelError("Адрес основной XUI_BASE_URL меняется через конфигурацию бота.")
        if panel.auth != "token" or not panel.api_token:
            raise PanelError("Для смены адреса нужен отдельный API-токен этой панели.")
        if any(k != key and p.base == url for k, (_, p) in runtime.xui_panels.items()):
            raise PanelError("Адрес уже используется другой панелью.")
        if panel.base == url:
            return
        candidate = XUIClient(url, api_token=panel.api_token)
        try:
            await candidate.server_status()
            await candidate.list_inbounds()
            old_profiles = {(c.username, c.raw.get("subId")) for c in await panel.list_clients(force=True)}
            new_profiles = {(c.username, c.raw.get("subId")) for c in await candidate.list_clients(force=True)}
            if not old_profiles or old_profiles != new_profiles or any(not sid for _, sid in old_profiles):
                raise PanelError("Не удалось подтвердить, что новый адрес обслуживает те же подписки.")
        finally:
            await candidate.close()
        records = _records()
        records.setdefault(key, {})["url"] = url
        db.set_setting(_setting, json.dumps(records))
        panel.base = url
        panel._invalidate()
