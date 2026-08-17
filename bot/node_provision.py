"""Авторазвёртывание нод: регистрация в Marzban + выдача cert по токену.

Отдельно от bot/panels/marzban.py: это админская инфраструктурная операция
(регистрация ноды), не часть абстракции «клиент панели» — 3x-ui-бэкенд этого
не умеет и не должен. Используется только когда PANEL_BACKEND=marzban.
"""
from __future__ import annotations

import logging
import secrets

import aiohttp

from .config import config

log = logging.getLogger("node_provision")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_SERVICE_PORT = 62050
_XRAY_API_PORT = 62051


class ProvisionError(Exception):
    pass


async def _marzban_auth(session: aiohttp.ClientSession) -> str:
    async with session.post(
        f"{config.marzban_url}/api/admin/token",
        data={"username": config.marzban_username, "password": config.marzban_password},
        headers={"User-Agent": _UA},
    ) as r:
        data = await r.json(content_type=None)
    tok = (data or {}).get("access_token")
    if not tok:
        raise ProvisionError(f"логин в панель не удался: {data}")
    return tok


async def _detect_panel_ip() -> str:
    """IP, с которого панель стучится к ноде (для UFW-скоупа 62050/62051 на
    ноде). Бот сейчас всегда живёт на той же машине, что и панель — поэтому
    свой собственный внешний IP и есть искомый origin-IP панели. Если бот
    когда-нибудь переедет на отдельный хост от панели — это надо поменять на
    явный конфиг, авто-детект перестанет быть верным."""
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=10)) as r:
            return (await r.text()).strip()


async def register_node(name: str, address: str) -> dict:
    """Регистрирует ноду в Marzban, тянет её client-cert. Возвращает
    {node_id, cert_pem, panel_ip, service_port, xray_api_port}."""
    async with aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}

        async with s.post(
            f"{config.marzban_url}/api/node",
            json={
                "name": name, "address": address,
                "port": _SERVICE_PORT, "api_port": _XRAY_API_PORT,
                "add_as_new_host": True,
            },
            headers=headers,
        ) as r:
            reg = await r.json(content_type=None)
            if r.status >= 400:
                raise ProvisionError(f"регистрация ноды не удалась ({r.status}): {reg}")
        node_id = reg.get("id")
        if not node_id:
            raise ProvisionError(f"панель не вернула id ноды: {reg}")

        async with s.get(f"{config.marzban_url}/api/node/settings", headers=headers) as r:
            settings = await r.json(content_type=None)
            if r.status >= 400:
                raise ProvisionError(f"не смог получить cert панели ({r.status}): {settings}")
        cert = settings.get("certificate")
        if not cert:
            raise ProvisionError("панель вернула пустой cert")

    panel_ip = await _detect_panel_ip()
    return {
        "node_id": node_id,
        "cert_pem": cert,
        "panel_ip": panel_ip,
        "service_port": _SERVICE_PORT,
        "xray_api_port": _XRAY_API_PORT,
    }


def new_token() -> str:
    return secrets.token_urlsafe(32)
