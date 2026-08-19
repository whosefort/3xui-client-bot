"""Авторазвёртывание нод: регистрация в Marzban + выдача cert по токену.

Отдельно от bot/panels/marzban.py: это админская инфраструктурная операция
(регистрация ноды), не часть абстракции «клиент панели» — 3x-ui-бэкенд этого
не умеет и не должен. Используется только когда PANEL_BACKEND=marzban.
"""
from __future__ import annotations

import asyncio
import logging
import secrets

import aiohttp

from .config import config

log = logging.getLogger("node_provision")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_SERVICE_PORT = 62050
_XRAY_API_PORT = 62051

# Общий на весь модуль + bot/reality_admin.py лок вокруг ЛЮБОЙ мутации
# /api/core/config или /api/hosts. Обе — не CRUD, а bulk GET-целиком →
# правим в памяти → PUT-целиком-обратно; Marzban не даёт ни ETag, ни
# версионирования. Без лока два параллельных admin-действия (два админа в
# ADMIN_IDS, или просто быстрый двойной тап) могут тихо затереть результат
# друг друга — molчаливый lost update, не ошибка.
config_lock = asyncio.Lock()


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


async def list_nodes() -> list[dict]:
    """Реальные ноды из /api/nodes (не хосты-в-подписке — здесь address —
    настоящий IP ноды, куда бот будет ходить по SSH)."""
    async with aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        async with s.get(f"{config.marzban_url}/api/nodes", headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise ProvisionError(f"не смог получить список нод ({r.status}): {data}")
            return data


async def list_servers() -> list[dict]:
    """Плоский список хостов из /api/hosts — то, что реально видит клиент в
    своём приложении (remark = имя сервера в списке). Один узел (нода) обычно
    даёт один host на инбаунд; при нескольких инбаундах на узле будет
    несколько записей. {tag, index, remark, address} — tag+index достаточно,
    чтобы однозначно адресовать запись в bulk-PUT /api/hosts (Marzban не даёт
    отдельного id хосту)."""
    async with aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise ProvisionError(f"не смог получить список хостов ({r.status}): {data}")

    out = []
    for tag, hosts in (data or {}).items():
        for i, h in enumerate(hosts):
            out.append({
                "tag": tag, "index": i,
                "remark": h.get("remark") or "",
                "address": h.get("address") or "",
                "sni": h.get("sni") or "",
                "fragment": bool(h.get("fragment_setting")),
                "fp": h.get("fingerprint") or "none",
            })
    return out


async def rename_server(tag: str, index: int, remark: str) -> None:
    """Меняет remark одного хоста — bulk-эндпоинт, поэтому тянем всю
    структуру /api/hosts, правим один элемент и кладём обратно целиком."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise ProvisionError(f"не смог получить список хостов ({r.status}): {data}")

        hosts = (data or {}).get(tag)
        if not hosts or index >= len(hosts):
            raise ProvisionError(f"хост {tag}[{index}] не найден — список хостов изменился")
        hosts[index]["remark"] = remark

        async with s.put(f"{config.marzban_url}/api/hosts", json=data, headers=headers) as r:
            res = await r.json(content_type=None)
            if r.status >= 400:
                raise ProvisionError(f"не смог сохранить remark ({r.status}): {res}")
