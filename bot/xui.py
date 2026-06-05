"""Клиент 3X-UI под первоклассный Clients API (выверено на живой панели).

Авторизация: заголовок `Authorization: Bearer <token>` — работает для всех
эндпоинтов /panel/api/*. Логин/пароль не нужен (но поддержан как запасной).

Подтверждённые на живой панели формы:
- GET  /panel/api/clients/list            -> {success, obj:[client,...]}
- POST /panel/api/clients/add             {"client": {...}, "inboundIds":[1]}
- POST /panel/api/clients/update/{email}  <голый объект client> (без обёртки)
- POST /panel/api/clients/del/{email}     (без тела)
Объект client: email, uuid, subId, tgId(int), expiryTime(мс,0=бессрочно),
enable, totalGB(БАЙТЫ), inboundIds[], flow, security, limitIp, group, comment,
reset, + вложенный traffic{up,down,total,lastOnline}.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import uuid
from typing import Any, Optional

import aiohttp

log = logging.getLogger("xui")

# Поля клиента, которые безопасно слать на запись (без вложенных traffic/reverse/*At).
_WRITABLE = {"email", "uuid", "subId", "flow", "security", "limitIp", "totalGB",
             "expiryTime", "enable", "tgId", "group", "comment", "password",
             "auth", "reset"}


class XUIError(Exception):
    pass


class XUIClient:
    def __init__(self, base_url: str, *, auth: str = "token", api_token: str = "",
                 username: str = "", password: str = "", twofa_secret: str = "",
                 client_flow: str = "xtls-rprx-vision", verify_ssl: bool = True):
        self.base = base_url.rstrip("/")
        self.auth = auth
        self.api_token = api_token
        self.username = username
        self.password = password
        self.twofa_secret = twofa_secret
        self.client_flow = client_flow
        self._verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._logged_in = False

    # ---------- сессия / авторизация ----------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx: Any = None
            if not self._verify_ssl:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx is not None else None
            headers = {}
            if self.auth == "token" and self.api_token:
                headers["Authorization"] = f"Bearer {self.api_token}"
            self._session = aiohttp.ClientSession(connector=connector, headers=headers)
        return self._session

    async def _login_if_needed(self) -> None:
        if self.auth == "token":
            return
        if self._logged_in:
            return
        session = await self._ensure_session()
        payload = {"username": self.username, "password": self.password}
        if self.twofa_secret:
            payload["twoFactorCode"] = self._current_2fa()
        async with session.post(f"{self.base}/login", data=payload) as r:
            data = await r.json(content_type=None)
        if not data or not data.get("success"):
            raise XUIError(f"Логин в панель не удался: {data}")
        self._logged_in = True

    def _current_2fa(self) -> str:
        import base64
        import hashlib
        import hmac
        import struct
        key = base64.b32decode(self.twofa_secret.upper() + "=" * (-len(self.twofa_secret) % 8))
        counter = struct.pack(">Q", int(time.time()) // 30)
        digest = hmac.new(key, counter, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{code:06d}"

    async def _request(self, method: str, path: str, *, json_body: Any = None) -> dict:
        await self._login_if_needed()
        session = await self._ensure_session()
        kw = {}
        if json_body is not None:
            kw["json"] = json_body
        async with session.request(method, f"{self.base}{path}", **kw) as r:
            text = await r.text()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise XUIError(f"{method} {path} -> не JSON (код {r.status}): {text[:160]}")
        if isinstance(data, dict) and data.get("success") is False:
            raise XUIError(f"{method} {path} -> {data.get('msg')}")
        return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- ЧТЕНИЕ ----------

    async def list_clients(self) -> list[dict]:
        """Все клиенты главной панели (первоклассный Clients API)."""
        data = await self._request("GET", "/panel/api/clients/list")
        return data.get("obj") or []

    async def find_by_tgid(self, tg_id: int) -> Optional[dict]:
        for c in await self.list_clients():
            if int(c.get("tgId") or 0) == int(tg_id):
                return c
        return None

    async def find_by_email(self, email: str) -> Optional[dict]:
        for c in await self.list_clients():
            if c.get("email") == email:
                return c
        return None

    # ---------- ЗАПИСЬ ----------

    async def create_client(self, *, tg_id: int, email: str, days: int, traffic_gb: int,
                            inbound_ids: list[int]) -> dict:
        if not inbound_ids:
            raise XUIError("Не задан DEFAULT_INBOUND_IDS — некуда вешать клиента")
        sub_id = uuid.uuid4().hex[:16]
        client = {
            "email": email,
            "uuid": str(uuid.uuid4()),
            "subId": sub_id,
            "flow": self.client_flow,
            "security": "auto",
            "limitIp": 0,
            "totalGB": int(traffic_gb) * 1024 ** 3,   # хранится в БАЙТАХ
            "expiryTime": int((time.time() + days * 86400) * 1000),
            "enable": True,
            "tgId": int(tg_id),
            "group": "",
            "comment": "",
            "reset": 0,
        }
        await self._request("POST", "/panel/api/clients/add",
                            json_body={"client": client, "inboundIds": list(inbound_ids)})
        return {"email": email, "sub_id": sub_id, "expiry_ms": client["expiryTime"]}

    async def extend_client(self, *, client: dict, add_days: int) -> dict:
        """Продлить от текущего конца (или от сейчас, если уже истёк)."""
        now_ms = int(time.time() * 1000)
        cur = int(client.get("expiryTime") or 0)
        base = cur if cur > now_ms else now_ms
        new_exp = base + add_days * 86400 * 1000
        body = self._writable(client)
        body["expiryTime"] = new_exp
        body["enable"] = True
        await self._request("POST", f"/panel/api/clients/update/{client['email']}",
                            json_body=body)
        return {"expiry_ms": new_exp}

    async def set_enabled(self, *, client: dict, enabled: bool) -> None:
        body = self._writable(client)
        body["enable"] = enabled
        await self._request("POST", f"/panel/api/clients/update/{client['email']}",
                            json_body=body)

    async def delete_client(self, email: str) -> None:
        await self._request("POST", f"/panel/api/clients/del/{email}")

    @staticmethod
    def _writable(client: dict) -> dict:
        return {k: v for k, v in client.items() if k in _WRITABLE}

    # ---------- утилиты ----------

    def sub_url(self, sub_id: str, template: str = "") -> str:
        """Ссылка-подписка. Формат панели задаётся SUB_URL_TEMPLATE c {subId}."""
        if template:
            return template.replace("{subId}", sub_id)
        return f"{self.base}/sub/{sub_id}"

    @staticmethod
    def days_left(client: dict) -> Optional[int]:
        exp = int(client.get("expiryTime") or 0)
        if exp <= 0:
            return None  # бессрочно
        return max(0, int((exp / 1000 - time.time()) // 86400))
