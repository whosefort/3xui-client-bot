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
from urllib.parse import quote

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
        # Короткоживущий кэш списка клиентов: гасит «громовое стадо» при спаме
        # /status (каждый resolve_client иначе тянет полный список 1-2 раза).
        self._clients_cache: Optional[list[dict]] = None
        self._clients_cache_ts = 0.0
        self._cache_ttl = 3.0  # сек

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
            # Явные таймауты: зависшая панель не должна держать корутины бота
            # бесконечно (дефолт aiohttp по sock_read/connect не ограничен).
            timeout = aiohttp.ClientTimeout(total=30, sock_connect=10, sock_read=20)
            self._session = aiohttp.ClientSession(
                connector=connector, headers=headers, timeout=timeout)
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

    async def list_clients(self, *, force: bool = False) -> list[dict]:
        """Все клиенты главной панели (первоклассный Clients API).

        Кэшируется на _cache_ttl секунд. Возвращаем копию, чтобы вызыватели
        (сортировка в админке и т.п.) не мутировали кэш."""
        now = time.time()
        if (not force and self._clients_cache is not None
                and (now - self._clients_cache_ts) < self._cache_ttl):
            return list(self._clients_cache)
        data = await self._request("GET", "/panel/api/clients/list")
        if "obj" in data:
            clients = data.get("obj") or []
        elif data.get("success") is True:
            clients = []
        else:
            # Не маскируем странный ответ панели пустым списком (иначе админка
            # покажет «ноль клиентов», а find_by_* молча вернут None).
            raise XUIError("неожиданный ответ панели на list (нет поля obj)")
        self._clients_cache = clients
        self._clients_cache_ts = now
        return list(clients)

    def _invalidate_cache(self) -> None:
        self._clients_cache = None

    async def find_by_tgid(self, tg_id: int) -> Optional[dict]:
        # tgId<=0 — это «не привязан» (None→0). Никогда не матчим по нему, иначе
        # find_by_tgid(0) вернёт первого попавшегося непривязанного клиента.
        tg_id = int(tg_id)
        if tg_id <= 0:
            return None
        for c in await self.list_clients():
            try:
                cid = int(c.get("tgId") or 0)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid == tg_id:
                return c
        return None

    async def find_by_email(self, email: str) -> Optional[dict]:
        for c in await self.list_clients():
            if c.get("email") == email:
                return c
        return None

    async def find_by_subid(self, sub_id: str) -> Optional[dict]:
        sub_id = (sub_id or "").strip()
        if not sub_id:
            return None
        for c in await self.list_clients():
            if c.get("subId") == sub_id:
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
        self._invalidate_cache()
        return {"email": email, "sub_id": sub_id, "expiry_ms": client["expiryTime"]}

    async def extend_client(self, *, client: dict, add_days: int,
                            set_total_gb: Optional[int] = None, add_total_gb: int = 0,
                            reset_traffic: bool = False,
                            inbound_ids: Optional[list[int]] = None) -> dict:
        """Продлить подписку (конец считается от текущего конца, или от сейчас если истёк).

        Лимит трафика:
          - set_total_gb задан  → ЖЁСТКО выставить лимит (ГБ). Месячное обновление: 150.
          - иначе add_total_gb  → ПРИБАВИТЬ к текущему лимиту (ГБ). Ручное N-мес продление.
          - оба не заданы       → лимит не трогаем.
        reset_traffic=True → обнулить счётчик использованного (свежий месяц).
        inbound_ids — где сбрасывать счётчик (fallback к client['inboundIds'])."""
        now_ms = int(time.time() * 1000)
        cur = int(client.get("expiryTime") or 0)
        base = cur if cur > now_ms else now_ms
        new_exp = base + add_days * 86400 * 1000
        body = self._writable(client)
        body["expiryTime"] = new_exp
        body["enable"] = True
        if set_total_gb is not None:
            body["totalGB"] = int(set_total_gb) * 1024 ** 3
        elif add_total_gb:
            body["totalGB"] = int(client.get("totalGB") or 0) + int(add_total_gb) * 1024 ** 3
        await self._request("POST", f"/panel/api/clients/update/{quote(client['email'], safe='')}",
                            json_body=body)
        self._invalidate_cache()
        if reset_traffic:
            ids = [int(i) for i in (client.get("inboundIds") or inbound_ids or [])]
            await self.reset_client_traffic(email=client["email"], inbound_ids=ids)
        return {"expiry_ms": new_exp,
                "total_gb_bytes": int(body.get("totalGB", client.get("totalGB") or 0))}

    async def reset_client_traffic(self, *, email: str, inbound_ids: list[int]) -> int:
        """Обнулить счётчик трафика клиента. 3X-UI считает трафик пер-инбаунд,
        поэтому сбрасываем на каждом inbound клиента. Возвращает число успешных
        сбросов. Несуществующий клиент на каком-то inbound — не ошибка, пропускаем."""
        ok = 0
        for iid in inbound_ids:
            try:
                await self._request(
                    "POST",
                    f"/panel/api/inbounds/{int(iid)}/resetClientTraffic/{quote(email, safe='')}")
                ok += 1
            except XUIError as e:
                log.warning("reset traffic inbound=%s email=%s: %s", iid, email, e)
        if ok:
            self._invalidate_cache()
        else:
            log.error("reset traffic: НИ ОДИН inbound не сброшен для %s (ids=%s)", email, inbound_ids)
        return ok

    async def bind_tgid(self, *, client: dict, tg_id: int) -> None:
        """Проставить клиенту tgId (усыновить уже существующего клиента ботом).
        Остальные поля (enable, лимит, срок) не трогаем."""
        body = self._writable(client)
        body["tgId"] = int(tg_id)
        await self._request("POST", f"/panel/api/clients/update/{quote(client['email'], safe='')}",
                            json_body=body)
        self._invalidate_cache()

    async def set_enabled(self, *, client: dict, enabled: bool) -> None:
        body = self._writable(client)
        body["enable"] = enabled
        await self._request("POST", f"/panel/api/clients/update/{quote(client['email'], safe='')}",
                            json_body=body)
        self._invalidate_cache()

    async def delete_client(self, email: str) -> None:
        await self._request("POST", f"/panel/api/clients/del/{quote(email, safe='')}")
        self._invalidate_cache()

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
        try:
            exp = int(client.get("expiryTime") or 0)
        except (TypeError, ValueError):
            return None
        if exp <= 0:
            return None  # бессрочно (или delayed-start — трактуем как безлимит)
        return max(0, int((exp / 1000 - time.time()) // 86400))

    @staticmethod
    def usage_bytes(client: dict) -> int:
        t = client.get("traffic") if isinstance(client.get("traffic"), dict) else client
        try:
            return int(t.get("up") or 0) + int(t.get("down") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0

    @staticmethod
    def is_exhausted(client: dict) -> bool:
        """Достигнут ли лимит трафика. totalGB==0 => безлимит."""
        try:
            total = int(client.get("totalGB") or 0)
        except (TypeError, ValueError):
            return False
        if total <= 0:
            return False
        return XUIClient.usage_bytes(client) >= total
