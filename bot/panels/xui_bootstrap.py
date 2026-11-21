"""Bootstrap an explicitly enrolled empty VPS over pinned SSH."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import os
import re
import shlex
import socket
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import asyncssh

from .. import runtime
from . import xui_registry
from .base import PanelError

_script = Path(__file__).resolve().parents[2] / "xui" / "bootstrap_managed.sh"
_provision_lock = asyncio.Lock()


async def setup(key: str, address: str, port: int, domain: str = "") -> None:
    xui_registry.validate_key(key)
    xui_registry.registry_cipher()
    try:
        host = ipaddress.IPv4Address(address)
    except ValueError:
        raise PanelError("Укажите публичный IPv4 VPS.") from None
    if not host.is_global or not 1 <= port <= 65535:
        raise PanelError("Укажите публичный IPv4 и корректный SSH-порт.")
    domain = domain.lower().rstrip(".")
    ip_certificate = not domain
    if domain and (len(domain) > 253 or not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain
    )):
        raise PanelError("Укажите домен VPS, например vpn.example.org.")
    image = os.getenv("XUI_BOOTSTRAP_IMAGE", "")
    key_file = os.getenv("XUI_BOOTSTRAP_SSH_KEY", "")
    hosts_file = os.getenv("XUI_BOOTSTRAP_KNOWN_HOSTS", "")
    if not re.fullmatch(r"ghcr\.io/mhsanaei/3x-ui@sha256:[a-f0-9]{64}", image):
        raise PanelError("Настройте XUI_BOOTSTRAP_IMAGE с проверенным digest 3x-ui.")
    if not key_file or not hosts_file or not Path(key_file).is_file() or not Path(hosts_file).is_file():
        raise PanelError("Настройте SSH-ключ бота и known_hosts для автосетапа.")
    if domain:
        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(loop.getaddrinfo(domain, None, type=socket.SOCK_STREAM), 10)
        except (OSError, TimeoutError):
            raise PanelError("DNS-имя VPS не разрешается.") from None
        if {record[4][0] for record in records} != {str(host)}:
            raise PanelError("DNS домена должен указывать только на IPv4 этого VPS.")
    else:
        domain = str(host)
    async with _provision_lock:
        if key in runtime.xui_panels:
            raise PanelError("Идентификатор уже занят.")
        if any(urlsplit(p.base).hostname == domain
               for _, p in runtime.xui_panels.values()):
            raise PanelError("На домене уже зарегистрирована панель.")
        command = shlex.join(["bash", "-s", "--", domain, image, "ip" if ip_certificate else "dns"])
        try:
            async with asyncssh.connect(str(host), port=port, username="root", client_keys=[key_file],
                                        known_hosts=hosts_file, agent_path=None,
                                        connect_timeout=12, login_timeout=12, keepalive_interval=10) as conn:
                result = await conn.run(command, input=_script.read_text(), check=True, timeout=630)
        except asyncssh.ProcessError as exc:
            phase = re.search(r"3x-ui setup failed: ([a-z]+)", exc.stderr or "")
            suffix = f" Этап: {phase[1]}." if phase else ""
            raise PanelError("Установка остановилась." + suffix) from None
        except (asyncssh.Error, OSError, TimeoutError):
            raise PanelError("Установка остановилась. Проверьте SSH и состояние VPS перед повтором.") from None
        values = result.stdout.strip().splitlines()
        if (len(values) != 3 or not re.fullmatch(r"[a-f0-9]{32}", values[0])
                or not re.fullmatch(r"[A-Za-z0-9_-]{24,}", values[1])
                or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+/", values[2])):
            raise PanelError("Установщик не подтвердил адрес и токен панели.")
        url = f"https://{domain}/{values[0]}"
        for attempt in range(12):
            try:
                await xui_registry.register(key, key, url, f"https://{domain}{values[2]}{{subId}}",
                                            generated_token=values[1])
                return
            except PanelError:
                if attempt == 11:
                    raise
                await asyncio.sleep(5)


async def verify_subscription(url: str, port: int) -> None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url, headers={"User-Agent": "Happ/3.0"},
                                   allow_redirects=False) as response:
                if response.status != 200:
                    raise PanelError("HTTPS-ссылка подписки не открывается.")
                body = (await response.read()).strip()
        if not body.startswith(b"vless://"):
            body = base64.b64decode(body + b"=" * (-len(body) % 4), validate=True)
        links = (urlsplit(line) for line in body.decode().splitlines() if line.startswith("vless://"))
        if any(link.port == port and link.hostname for link in links):
            return
    except (aiohttp.ClientError, TimeoutError, ValueError, binascii.Error):
        pass
    raise PanelError("Подписка не вернула VLESS-профиль на порту первого входа.")
