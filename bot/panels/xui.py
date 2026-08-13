"""Бэкенд 3x-ui (первоклассный Clients API). Тот же движок, что раньше, но под
общий интерфейс PanelClient и с нормализацией в Client.

identity = email (= u{tg_id}). Срок в 3x-ui — мс, отдаём наружу в секундах.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import uuid
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from .base import Client, PanelClient, PanelError

log = logging.getLogger("xui")

_WRITABLE = {"email", "uuid", "subId", "flow", "security", "limitIp", "totalGB",
             "expiryTime", "enable", "tgId", "group", "comment", "password", "auth", "reset"}
_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10, sock_read=20)


class XUIClient(PanelClient):
    def __init__(self, base_url: str, *, auth: str = "token", api_token: str = "",
                 username: str = "", password: str = "", twofa_secret: str = "",
                 client_flow: str = "xtls-rprx-vision", sub_url_template: str = "",
                 inbound_ids: Optional[list[int]] = None, verify_ssl: bool = True):
        self.base = base_url.rstrip("/")
        self.auth = auth
        self.api_token = api_token
        self.username = username
        self.password = password
        self.twofa_secret = twofa_secret
        self.client_flow = client_flow
        self.sub_url_template = sub_url_template
        self.inbound_ids = inbound_ids or [1]
        self._verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._logged_in = False
        self._cache: Optional[list[Client]] = None
        self._cache_ts = 0.0
        self._cache_ttl = 3.0

    # ---------- сессия / авторизация ----------

    async def _session_(self) -> aiohttp.ClientSession:
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
            self._session = aiohttp.ClientSession(connector=connector, headers=headers, timeout=_TIMEOUT)
        return self._session

    async def _login_if_needed(self) -> None:
        if self.auth == "token" or self._logged_in:
            return
        s = await self._session_()
        payload = {"username": self.username, "password": self.password}
        if self.twofa_secret:
            payload["twoFactorCode"] = self._current_2fa()
        async with s.post(f"{self.base}/login", data=payload) as r:
            data = await r.json(content_type=None)
        if not data or not data.get("success"):
            raise PanelError(f"Логин в 3x-ui не удался: {data}")
        self._logged_in = True

    def _current_2fa(self) -> str:
        import base64, hashlib, hmac, struct
        key = base64.b32decode(self.twofa_secret.upper() + "=" * (-len(self.twofa_secret) % 8))
        counter = struct.pack(">Q", int(time.time()) // 30)
        digest = hmac.new(key, counter, hashlib.sha1).digest()
        off = digest[-1] & 0x0F
        code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{code:06d}"

    async def _request(self, method: str, path: str, *, json_body: Any = None) -> dict:
        await self._login_if_needed()
        s = await self._session_()
        kw = {}
        if json_body is not None:
            kw["json"] = json_body
        async with s.request(method, f"{self.base}{path}", **kw) as r:
            text = await r.text()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise PanelError(f"{method} {path} -> не JSON (код {r.status}): {text[:160]}")
        if isinstance(data, dict) and data.get("success") is False:
            raise PanelError(f"{method} {path} -> {data.get('msg')}")
        return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- утилиты ----------

    def _sub_url(self, sub_id: str) -> str:
        if self.sub_url_template:
            return self.sub_url_template.replace("{subId}", sub_id)
        return f"{self.base}/sub/{sub_id}"

    def _to_client(self, c: dict) -> Client:
        exp_ms = int(c.get("expiryTime") or 0)
        t = c.get("traffic") if isinstance(c.get("traffic"), dict) else c
        try:
            used = int(t.get("up") or 0) + int(t.get("down") or 0)
        except (TypeError, ValueError, AttributeError):
            used = 0
        return Client(
            username=c.get("email", ""),
            tg_id=int(c.get("tgId") or 0),
            sub_url=self._sub_url(c.get("subId") or ""),
            enabled=bool(c.get("enable", True)),
            expire_ts=exp_ms // 1000 if exp_ms > 0 else 0,
            used_bytes=used,
            limit_bytes=int(c.get("totalGB") or 0),
            raw=c,
        )

    @staticmethod
    def _writable(client: dict) -> dict:
        return {k: v for k, v in client.items() if k in _WRITABLE}

    def _invalidate(self) -> None:
        self._cache = None

    # ---------- чтение ----------

    async def list_clients(self, *, force: bool = False) -> list[Client]:
        now = time.time()
        if not force and self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return list(self._cache)
        data = await self._request("GET", "/panel/api/clients/list")
        if "obj" in data:
            raw = data.get("obj") or []
        elif data.get("success") is True:
            raw = []
        else:
            raise PanelError("неожиданный ответ 3x-ui на list (нет obj)")
        self._cache = [self._to_client(c) for c in raw]
        self._cache_ts = now
        return list(self._cache)

    async def find_by_username(self, username: str) -> Optional[Client]:
        for c in await self.list_clients():
            if c.username == username:
                return c
        return None

    async def find_by_tgid(self, tg_id: int) -> Optional[Client]:
        tg_id = int(tg_id)
        if tg_id <= 0:
            return None
        for c in await self.list_clients():
            if c.tg_id > 0 and c.tg_id == tg_id:
                return c
        return None

    async def find_by_subid(self, needle: str) -> Optional[Client]:
        needle = (needle or "").strip()
        if not needle:
            return None
        for c in await self.list_clients():
            if needle == (c.raw.get("subId") or "") or needle in c.sub_url:
                return c
        return None

    # ---------- запись ----------

    async def create_client(self, *, tg_id: int, days: int, traffic_gb: int) -> Client:
        email = self.username_for(tg_id)
        sub_id = uuid.uuid4().hex[:16]
        client = {
            "email": email, "uuid": str(uuid.uuid4()), "subId": sub_id,
            "flow": self.client_flow, "security": "auto", "limitIp": 0,
            "totalGB": int(traffic_gb) * 1024 ** 3,
            "expiryTime": int((time.time() + days * 86400) * 1000),
            "enable": True, "tgId": int(tg_id), "group": "", "comment": "", "reset": 0,
        }
        await self._request("POST", "/panel/api/clients/add",
                            json_body={"client": client, "inboundIds": list(self.inbound_ids)})
        self._invalidate()
        return self._to_client(client)

    async def extend_client(self, *, client: Client, add_days: int,
                            set_total_gb: Optional[int] = None, add_total_gb: int = 0,
                            reset_traffic: bool = False) -> Client:
        raw = dict(client.raw)
        now_ms = int(time.time() * 1000)
        cur = int(raw.get("expiryTime") or 0)
        base = cur if cur > now_ms else now_ms
        new_exp = max(base + add_days * 86400 * 1000, now_ms)
        body = self._writable(raw)
        body["expiryTime"] = new_exp
        body["enable"] = True
        if set_total_gb is not None:
            body["totalGB"] = int(set_total_gb) * 1024 ** 3
        elif add_total_gb:
            body["totalGB"] = int(raw.get("totalGB") or 0) + int(add_total_gb) * 1024 ** 3
        await self._request("POST", f"/panel/api/clients/update/{quote(client.username, safe='')}",
                            json_body=body)
        self._invalidate()
        if reset_traffic:
            await self._reset_inbounds(client.username, raw.get("inboundIds") or self.inbound_ids)
        return await self.find_by_username(client.username) or self._to_client({**raw, **body})

    async def _reset_inbounds(self, email: str, inbound_ids) -> None:
        for iid in inbound_ids:
            try:
                await self._request(
                    "POST", f"/panel/api/inbounds/{int(iid)}/resetClientTraffic/{quote(email, safe='')}")
            except PanelError as e:
                log.warning("reset traffic inbound=%s email=%s: %s", iid, email, e)

    async def set_enabled(self, *, client: Client, enabled: bool) -> None:
        body = self._writable(client.raw)
        body["enable"] = enabled
        await self._request("POST", f"/panel/api/clients/update/{quote(client.username, safe='')}",
                            json_body=body)
        self._invalidate()

    async def delete_client(self, username: str) -> None:
        await self._request("POST", f"/panel/api/clients/del/{quote(username, safe='')}")
        self._invalidate()

    async def reset_traffic(self, *, client: Client) -> None:
        await self._reset_inbounds(client.username, client.raw.get("inboundIds") or self.inbound_ids)
        self._invalidate()

    async def bind_tgid(self, *, client: Client, tg_id: int) -> None:
        body = self._writable(client.raw)
        body["tgId"] = int(tg_id)
        await self._request("POST", f"/panel/api/clients/update/{quote(client.username, safe='')}",
                            json_body=body)
        self._invalidate()
