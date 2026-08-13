"""Бэкенд Marzban (выверено на живой панели v-current).

Контракт (подтверждён):
- POST /api/admin/token            form username/password -> {access_token}
- GET  /api/inbounds               {protocol: [{tag,...}]}
- GET  /api/users?offset&limit     {total, users:[user]}
- GET/POST/PUT/DELETE /api/user[/{username}]
- POST /api/user/{username}/reset  сброс использованного трафика
user: username, status(active|disabled|limited|expired|on_hold), used_traffic,
data_limit(байты, null=безлимит), data_limit_reset_strategy(no_reset|day|week|month|year),
expire(unix-сек, null), proxies{}, inbounds{proto:[tag]}, subscription_url, links[], note.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import aiohttp

from .base import Client, PanelClient, PanelError

log = logging.getLogger("marzban")

_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10, sock_read=20)


class MarzbanClient(PanelClient):
    def __init__(self, base_url: str, username: str, password: str, *,
                 reset_strategy: str = "month",
                 proxies: Optional[dict] = None, inbounds: Optional[dict] = None,
                 verify_ssl: bool = True):
        self.base = base_url.rstrip("/")
        self._user = username
        self._pass = password
        self.reset_strategy = reset_strategy
        # если proxies/inbounds не заданы — соберём из /api/inbounds (все доступные)
        self._proxies_cfg = proxies
        self._inbounds_cfg = inbounds
        self._verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: str = ""
        self._inbound_map: Optional[dict] = None    # {proto: [tags]}
        self._cache: Optional[list[Client]] = None
        self._cache_ts = 0.0
        self._cache_ttl = 3.0

    # ---------- сессия / токен ----------

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def _auth(self) -> None:
        s = await self._session_()
        async with s.post(f"{self.base}/api/admin/token",
                          data={"username": self._user, "password": self._pass},
                          ssl=self._verify_ssl) as r:
            data = await r.json(content_type=None)
        tok = (data or {}).get("access_token")
        if not tok:
            raise PanelError(f"Marzban: логин не удался ({data})")
        self._token = tok

    async def _request(self, method: str, path: str, *, json_body: Any = None,
                       _retry: bool = True) -> Any:
        if not self._token:
            await self._auth()
        s = await self._session_()
        kw: dict = {"ssl": self._verify_ssl, "headers": {"Authorization": f"Bearer {self._token}"}}
        if json_body is not None:
            kw["json"] = json_body
        async with s.request(method, f"{self.base}{path}", **kw) as r:
            status = r.status
            text = await r.text()
        if status == 401 and _retry:            # токен протух — перелогин и один ретрай
            await self._auth()
            return await self._request(method, path, json_body=json_body, _retry=False)
        if status == 404:
            return None
        if status >= 400:
            raise PanelError(f"Marzban {method} {path} -> {status}: {text[:200]}")
        return _loads(text)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- inbounds/proxies по умолчанию ----------

    async def _defaults(self) -> tuple[dict, dict]:
        """(proxies, inbounds) для создания юзера. Если не задано в конфиге —
        берём ВСЕ инбаунды панели (новый инбаунд в панели → новые юзеры его получат)."""
        if self._proxies_cfg and self._inbounds_cfg:
            return self._proxies_cfg, self._inbounds_cfg
        if self._inbound_map is None:
            data = await self._request("GET", "/api/inbounds") or {}
            self._inbound_map = {proto: [i["tag"] for i in items]
                                 for proto, items in data.items()}
        proxies = self._proxies_cfg or {proto: {} for proto in self._inbound_map}
        inbounds = self._inbounds_cfg or dict(self._inbound_map)
        return proxies, inbounds

    # ---------- маппинг ----------

    def _to_client(self, u: dict) -> Client:
        note = (u.get("note") or "").strip()
        tg = 0
        if note.startswith("tg:"):
            try:
                tg = int(note[3:].strip())
            except ValueError:
                tg = 0
        if not tg:
            un = u.get("username", "")
            if un.startswith("u") and un[1:].isdigit():
                tg = int(un[1:])
        sub = u.get("subscription_url", "") or ""
        if sub and not sub.startswith("http"):
            sub = f"{self.base}{sub}"
        return Client(
            username=u.get("username", ""),
            tg_id=tg,
            sub_url=sub,
            enabled=(u.get("status") == "active"),
            expire_ts=int(u.get("expire") or 0),
            used_bytes=int(u.get("used_traffic") or 0),
            limit_bytes=int(u.get("data_limit") or 0),
            raw=u,
        )

    # ---------- чтение ----------

    async def list_clients(self, *, force: bool = False) -> list[Client]:
        now = time.time()
        if not force and self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return list(self._cache)
        out: list[Client] = []
        offset = 0
        while True:
            data = await self._request("GET", f"/api/users?offset={offset}&limit=200") or {}
            users = data.get("users") or []
            out.extend(self._to_client(u) for u in users)
            if len(users) < 200:
                break
            offset += 200
        self._cache = out
        self._cache_ts = now
        return list(out)

    def _invalidate(self) -> None:
        self._cache = None

    async def find_by_username(self, username: str) -> Optional[Client]:
        u = await self._request("GET", f"/api/user/{username}")
        return self._to_client(u) if u else None

    async def find_by_tgid(self, tg_id: int) -> Optional[Client]:
        tg_id = int(tg_id)
        if tg_id <= 0:
            return None
        # быстрый путь: наши юзеры называются u{tg_id}
        c = await self.find_by_username(self.username_for(tg_id))
        if c:
            return c
        # медленный: старый клиент, привязанный через note tg:{id}
        for c in await self.list_clients():
            if c.tg_id == tg_id:
                return c
        return None

    async def find_by_subid(self, needle: str) -> Optional[Client]:
        needle = (needle or "").strip()
        if not needle:
            return None
        for c in await self.list_clients():
            if needle and needle in c.sub_url:
                return c
        return None

    # ---------- запись ----------

    async def create_client(self, *, tg_id: int, days: int, traffic_gb: int) -> Client:
        proxies, inbounds = await self._defaults()
        body = {
            "username": self.username_for(tg_id),
            "proxies": proxies,
            "inbounds": inbounds,
            "expire": int(time.time()) + days * 86400,
            "data_limit": int(traffic_gb) * 1024 ** 3,
            "data_limit_reset_strategy": self.reset_strategy,   # month = авто-сброс
            "status": "active",
            "note": f"tg:{int(tg_id)}",
        }
        u = await self._request("POST", "/api/user", json_body=body)
        self._invalidate()
        return self._to_client(u)

    async def extend_client(self, *, client: Client, add_days: int,
                            set_total_gb: Optional[int] = None, add_total_gb: int = 0,
                            reset_traffic: bool = False) -> Client:
        now = int(time.time())
        cur = int(client.expire_ts or 0)
        base = cur if cur > now else now
        new_exp = max(base + add_days * 86400, now)     # не уводим в прошлое
        body: dict = {"expire": new_exp, "status": "active"}
        if set_total_gb is not None:
            body["data_limit"] = int(set_total_gb) * 1024 ** 3
        elif add_total_gb:
            body["data_limit"] = int(client.limit_bytes or 0) + int(add_total_gb) * 1024 ** 3
        u = await self._request("PUT", f"/api/user/{client.username}", json_body=body)
        if reset_traffic:
            await self._request("POST", f"/api/user/{client.username}/reset")
            u = await self._request("GET", f"/api/user/{client.username}") or u
        self._invalidate()
        return self._to_client(u)

    async def set_enabled(self, *, client: Client, enabled: bool) -> None:
        await self._request("PUT", f"/api/user/{client.username}",
                            json_body={"status": "active" if enabled else "disabled"})
        self._invalidate()

    async def delete_client(self, username: str) -> None:
        await self._request("DELETE", f"/api/user/{username}")
        self._invalidate()

    async def reset_traffic(self, *, client: Client) -> None:
        await self._request("POST", f"/api/user/{client.username}/reset")
        self._invalidate()

    async def bind_tgid(self, *, client: Client, tg_id: int) -> None:
        # identity в Marzban = username; tg_id держим в note для наглядности/поиска.
        await self._request("PUT", f"/api/user/{client.username}",
                            json_body={"note": f"tg:{int(tg_id)}"})
        self._invalidate()


def _loads(text: str) -> Any:
    import json
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise PanelError(f"Marzban: не JSON: {text[:160]}")
