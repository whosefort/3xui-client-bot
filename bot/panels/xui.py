"""Бэкенд 3x-ui (первоклассный Clients API). Тот же движок, что раньше, но под
общий интерфейс PanelClient и с нормализацией в Client.

identity = email (= u{tg_id}). Срок в 3x-ui — мс, отдаём наружу в секундах.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from .base import Client, PanelClient, PanelError

log = logging.getLogger("xui")

_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10, sock_read=20)
_BACKUP_TIMEOUT = aiohttp.ClientTimeout(total=180, sock_connect=10, sock_read=30)
_MAX_BACKUP_BYTES = 256 * 1024**2
_CLIENT_SETTINGS = {
    "totalGB", "expiryTime", "enable", "limitIp", "limitHwid", "group", "comment",
    "reset", "resetDay", "resetMax", "resetWeekday", "trafficReset", "trafficResetDay",
}


class XUIHTTPError(PanelError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"3x-ui вернула HTTP {status}.")


class XUIClient(PanelClient):
    def __init__(self, base_url: str, *, auth: str = "token", api_token: str = "",
                 username: str = "", password: str = "", twofa_secret: str = "",
                 client_flow: str = "xtls-rprx-vision", sub_url_template: str = "",
                 inbound_ids: Optional[list[int]] = None, verify_ssl: bool = True, exit_client: str = "",
                 node_token: str = ""):
        self.base = base_url.rstrip("/")
        self.auth = auth
        self.api_token = api_token
        self.username = username
        self.password = password
        self.twofa_secret = twofa_secret
        self.client_flow = client_flow
        self.sub_url_template = sub_url_template
        self.inbound_ids = inbound_ids or [1]
        self.exit_client = exit_client
        self.node_token = node_token
        self._verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._logged_in = False
        self.config_lock = asyncio.Lock()
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

    async def _request(self, method: str, path: str, *, json_body: Any = None, form: dict | None = None) -> dict:
        await self._login_if_needed()
        s = await self._session_()
        try:
            async with s.request(
                method, f"{self.base}{path}", json=json_body, data=form, allow_redirects=False,
            ) as r:
                if r.status < 200 or r.status >= 300:
                    raise XUIHTTPError(r.status)
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise PanelError(f"Не удалось получить ответ 3x-ui ({type(e).__name__}).") from e
        if not isinstance(data, dict) or data.get("success") is False:
            raise PanelError("3x-ui отклонила операцию.")
        return data

    @staticmethod
    def _object(value):
        while isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as e:
                raise PanelError("3x-ui вернула некорректный JSON.") from e
        return value

    async def list_inbounds(self) -> list[dict]:
        result = await self._request("GET", "/panel/api/inbounds/list")
        inbounds = self._object(result.get("obj")) or []
        for inbound in inbounds:
            for key in ("settings", "streamSettings"):
                inbound[key] = self._object(inbound.get(key)) or {}
        return inbounds

    async def server_status(self) -> dict:
        return self._object((await self._request("GET", "/panel/api/server/status")).get("obj")) or {}

    async def xray_template(self) -> tuple[dict, str | None]:
        response = await self._request("POST", "/panel/api/xray/", form={})
        obj = self._object(response.get("obj"))
        if not isinstance(obj, dict) or not isinstance(self._object(obj.get("xraySetting")), dict):
            raise PanelError("3x-ui не вернула шаблон Xray.")
        return self._object(obj["xraySetting"]), obj.get("outboundTestUrl")

    async def save_xray_template(self, template: dict, test_url: str | None) -> None:
        form = {"xraySetting": json.dumps(template)}
        if test_url is not None:
            form["outboundTestUrl"] = test_url
        await self._request("POST", "/panel/api/xray/update", form=form)

    async def restart_xray(self) -> None:
        await self._request("POST", "/panel/api/server/restartXrayService", form={})

    async def live_xray_config(self) -> dict:
        result = await self._request("GET", "/panel/api/server/getConfigJson")
        return self._object(result.get("obj")) or {}

    async def client_links(self, email: str) -> list[str]:
        response = await self._request("GET", f"/panel/api/clients/links/{quote(email, safe='')}")
        links = response.get("obj") or []
        return [link for link in links if isinstance(link, str)] if isinstance(links, list) else []

    @staticmethod
    def _client_path(email: str) -> str:
        if not email or email in (".", ".."):
            raise PanelError("Идентификатор клиента не подходит для этого метода 3x-ui.")
        return quote(email, safe="")

    async def _update_client(self, client: Client, change: dict) -> None:
        email = client.username
        # 3x-ui 3.11 overwrites omitted plan fields and flow. Do not send the
        # numeric record ID or credentials returned by the read API.
        settings = {key: value for key, value in client.raw.items()
                    if key in _CLIENT_SETTINGS and value is not None}
        body = {"email": email, "tgId": client.tg_id, **settings, **change}
        path = f"/panel/api/clients/update/{self._client_path(email)}"
        inbound_ids = client.raw.get("inboundIds") or []
        flows = {}
        if len(inbound_ids) > 1:
            for inbound in await self.list_inbounds():
                if inbound["id"] in inbound_ids:
                    record = next((c for c in inbound["settings"].get("clients", [])
                                   if c.get("email") == email), None)
                    if record is not None:
                        flows[inbound["id"]] = record.get("flow") or ""
            if set(flows) != set(inbound_ids):
                raise PanelError("Не удалось сверить flow на всех входах клиента.")
        self._invalidate()
        if len(set(flows.values())) > 1:
            for inbound_id, flow in flows.items():
                await self._request("POST", f"{path}?inboundIds={inbound_id}",
                                    json_body={**body, "flow": flow})
        else:
            flow = next(iter(flows.values()), client.raw.get("flow") or "")
            await self._request("POST", path, json_body={**body, "flow": flow})
        self._invalidate()

    async def hwids(self, email: str) -> list[dict]:
        reply = await self._request("POST", f"/panel/api/clients/hwids/{self._client_path(email)}", form={})
        values = self._object(reply.get("obj"))
        if values is None:
            return []
        if not isinstance(values, list) or not all(
            isinstance(device, dict) and isinstance(device.get("id"), int) for device in values
        ):
            raise PanelError("3x-ui вернула некорректный список устройств.")
        return values

    async def forget_hwids(self, email: str, device_id: int | None = None) -> None:
        async with self.config_lock:
            current = await self.hwids(email)
            if device_id is not None and not any(d["id"] == device_id for d in current):
                return
            if not current:
                return
            path = f"/panel/api/clients/hwids/{self._client_path(email)}"
            await self._request("DELETE", path if device_id is None else f"{path}/{device_id}")
            remaining = {device["id"] for device in await self.hwids(email)}
            expected_gone = {d["id"] for d in current} if device_id is None else {device_id}
            if remaining & expected_gone:
                raise PanelError("3x-ui не подтвердила отвязку устройств.")

    async def set_hwid_limit(self, email: str, limit: int) -> None:
        if not 0 <= limit <= 10000:
            raise PanelError("Лимит устройств должен быть от 0 до 10000.")
        async with self.config_lock:
            clients = await self.list_clients(force=True)
            client = next((c for c in clients if c.username == email), None)
            if client is None:
                raise PanelError("Клиент больше не существует.")
            if int(client.raw.get("limitHwid") or 0) != limit:
                await self._update_client(client, {"limitHwid": limit})
                clients = await self.list_clients(force=True)
                client = next((c for c in clients if c.username == email), None)
                if client is None or int(client.raw.get("limitHwid") or 0) != limit:
                    raise PanelError("3x-ui не подтвердила новый лимит устройств.")

    async def download_database(self, path: Path) -> None:
        """Stream a panel backup to a new file in the caller's private directory."""
        await self._login_if_needed()
        session = await self._session_()
        created = False
        try:
            async with session.get(f"{self.base}/panel/api/server/getDb", allow_redirects=False,
                                   timeout=_BACKUP_TIMEOUT) as response:
                if response.status != 200:
                    raise PanelError(f"Экспорт базы 3x-ui вернул HTTP {response.status}.")
                if response.content_length is not None and response.content_length > _MAX_BACKUP_BYTES:
                    raise PanelError("База 3x-ui больше 256 МБ.")
                with path.open("xb") as file:
                    created = True
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > _MAX_BACKUP_BYTES:
                            raise PanelError("База 3x-ui больше 256 МБ.")
                        file.write(chunk)
                    if size == 0:
                        raise PanelError("3x-ui вернула пустой бэкап.")
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise

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
            if needle == (c.raw.get("subId") or "") or c.matches_sub_id(needle):
                return c
        return None

    # ---------- запись ----------

    async def create_client(self, *, tg_id: int, days: int, traffic_gb: int,
                            username: str | None = None, inbound_id: int | None = None,
                            flow: str | None = None) -> Client:
        email = username or self.username_for(tg_id)
        sub_id = uuid.uuid4().hex[:16]
        client = {
            "email": email, "id": str(uuid.uuid4()), "subId": sub_id,
            "flow": self.client_flow if flow is None else flow, "security": "auto", "limitIp": 0,
            "totalGB": int(traffic_gb) * 1024 ** 3,
            "expiryTime": int((time.time() + days * 86400) * 1000),
            "enable": True, "tgId": int(tg_id), "group": "", "comment": "", "reset": 0,
        }
        await self._request("POST", "/panel/api/clients/add",
                            json_body={"client": client, "inboundIds": [inbound_id] if inbound_id is not None
                                       else list(self.inbound_ids)})
        self._invalidate()
        return self._to_client(client)

    async def extend_client(self, *, client: Client, add_days: int,
                            set_total_gb: Optional[int] = None, add_total_gb: int = 0,
                            reset_traffic: bool = False, expires_at: int | None = None) -> Client:
        raw = dict(client.raw)
        now_ms = int(time.time() * 1000)
        cur = int(raw.get("expiryTime") or 0)
        base = cur if cur > now_ms else now_ms
        new_exp = max(expires_at * 1000, cur) if expires_at is not None else max(base + add_days * 86400 * 1000, now_ms)
        body = {"expiryTime": new_exp, "enable": True}
        if set_total_gb is not None:
            body["totalGB"] = int(set_total_gb) * 1024 ** 3
        elif add_total_gb:
            body["totalGB"] = int(raw.get("totalGB") or 0) + int(add_total_gb) * 1024 ** 3
        await self._update_client(client, body)
        if reset_traffic:
            await self._reset_usage(client, strict=False)
        return await self.find_by_username(client.username) or self._to_client({**raw, **body})

    async def _reset_usage(self, client: Client, *, strict: bool) -> None:
        email = self._client_path(client.username)
        try:
            await self._request("POST", f"/panel/api/clients/resetTraffic/{email}", form={})
            return
        except XUIHTTPError as e:
            if e.status != 404:
                if strict:
                    raise
                log.warning("3x-ui reset traffic failed: HTTP %d", e.status)
                return
        except PanelError:
            if strict:
                raise
            log.warning("3x-ui reset traffic failed")
            return
        await self._reset_inbounds(client.username, client.raw.get("inboundIds") or self.inbound_ids, strict=strict)

    async def _reset_inbounds(self, email: str, inbound_ids, *, strict: bool = False) -> None:
        for iid in inbound_ids:
            try:
                await self._request(
                    "POST", f"/panel/api/inbounds/{int(iid)}/resetClientTraffic/{quote(email, safe='')}")
            except PanelError as e:
                if strict:
                    raise
                log.warning("reset traffic inbound=%s email=%s: %s", iid, email, e)

    async def set_enabled(self, *, client: Client, enabled: bool) -> None:
        await self._update_client(client, {"enable": enabled})

    async def set_unlimited(self, *, client: Client, reset_traffic: bool = True) -> Client:
        raw = dict(client.raw)
        body = {"expiryTime": 0, "totalGB": 0, "enable": True}
        await self._update_client(client, body)
        if reset_traffic:
            await self._reset_usage(client, strict=False)
        return await self.find_by_username(client.username) or self._to_client({**raw, **body})

    async def delete_client(self, username: str) -> None:
        await self._request("POST", f"/panel/api/clients/del/{quote(username, safe='')}")
        self._invalidate()

    async def reset_traffic(self, *, client: Client) -> None:
        await self._reset_usage(client, strict=True)
        self._invalidate()

    async def bind_tgid(self, *, client: Client, tg_id: int) -> None:
        await self._update_client(client, {"tgId": int(tg_id)})
