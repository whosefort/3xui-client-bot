"""Несколько панелей разом (например Marzban + отдельный сервер с 3x-ui).

Главная панель (первая в PANELS) — источник правды: списки, статус, сроки,
трафик в боте берутся из неё. Все записи (выдать, продлить, вкл/выкл,
безлимит, удалить, сброс трафика, привязка tg) повторяются во вторичных,
клиент там ищется по тому же username u{tg_id}; если его нет — создаётся с
тем же сроком/лимитом, что в главной (так подтягиваются старые клиенты,
выданные до подключения второй панели).

Сбой вторичной панели НЕ роняет операцию в главной: клиент получает доступ
хотя бы туда, ошибка уходит в лог и в last_warnings (админ видит её в боте).
Ссылки вторичных панелей кладутся в Client.extra_subs — клиент получает обе.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import Client, PanelClient, PanelError

log = logging.getLogger("multi")


class MultiPanel(PanelClient):
    def __init__(self, panels: list[tuple[str, PanelClient]]):
        assert panels, "нужна хотя бы одна панель"
        self.panels = panels                    # [(имя, клиент)], первая — главная
        self.primary_name, self.primary = panels[0]
        self.secondaries = panels[1:]
        self.last_warnings: list[str] = []

    # ---------- служебное ----------

    def _warn(self, name: str, what: str, e: Exception) -> None:
        msg = f"{name}: {what} не удалось — {e}"
        log.warning(msg)
        self.last_warnings = (self.last_warnings + [msg])[-20:]

    async def _enrich(self, cl: Optional[Client]) -> Optional[Client]:
        if cl is None or not self.secondaries:
            return cl
        extras = []
        for name, p in self.secondaries:
            try:
                other = await p.find_by_username(cl.username)
            except PanelError as e:
                self._warn(name, f"чтение {cl.username}", e)
                continue
            if other and other.sub_url:
                extras.append(other.sub_url)
        cl.extra_subs = extras
        return cl

    async def _ensure_secondary(self, name: str, p: PanelClient, primary: Client) -> Optional[Client]:
        """Клиент во вторичной панели; если нет — создаём по образу главной."""
        other = await p.find_by_username(primary.username)
        if other:
            return other
        tg = primary.tg_id or _tg_from_username(primary.username)
        if not tg:
            raise PanelError(f"не могу завести {primary.username}: нет tg_id")
        days = 3650 if primary.expire_ts <= 0 else max(1, int((primary.expire_ts - time.time()) // 86400) + 1)
        gb = primary.limit_bytes // 1024 ** 3 if primary.limit_bytes else 0
        other = await p.create_client(tg_id=tg, days=days, traffic_gb=gb)
        if primary.expire_ts <= 0 or not primary.limit_bytes:
            other = await p.set_unlimited(client=other, reset_traffic=False) if (
                primary.expire_ts <= 0 and not primary.limit_bytes) else other
        if not primary.enabled:
            await p.set_enabled(client=other, enabled=False)
        return other

    async def _mirror(self, primary: Client, what: str, op) -> None:
        for name, p in self.secondaries:
            try:
                other = await self._ensure_secondary(name, p, primary)
                if other is not None:
                    await op(p, other)
            except PanelError as e:
                self._warn(name, what, e)
            except Exception as e:  # noqa: BLE001 — вторичная не должна ронять главную
                self._warn(name, what, e)

    # ---------- чтение (главная) ----------

    async def close(self) -> None:
        for _, p in self.panels:
            await p.close()

    async def list_clients(self, *, force: bool = False) -> list[Client]:
        return await self.primary.list_clients(force=force)

    async def find_by_username(self, username: str) -> Optional[Client]:
        return await self._enrich(await self.primary.find_by_username(username))

    async def find_by_tgid(self, tg_id: int) -> Optional[Client]:
        return await self._enrich(await self.primary.find_by_tgid(tg_id))

    async def find_by_subid(self, needle: str) -> Optional[Client]:
        cl = await self.primary.find_by_subid(needle)
        if cl is None:
            # Клиент вставил ссылку вторичной панели — найдём его там и вернём
            # главную запись с тем же username.
            for name, p in self.secondaries:
                try:
                    other = await p.find_by_subid(needle)
                except PanelError as e:
                    self._warn(name, "поиск по ссылке", e)
                    continue
                if other:
                    cl = await self.primary.find_by_username(other.username)
                    if cl:
                        break
        return await self._enrich(cl)

    # ---------- запись (главная + зеркало) ----------

    async def create_client(self, *, tg_id: int, days: int, traffic_gb: int) -> Client:
        cl = await self.primary.create_client(tg_id=tg_id, days=days, traffic_gb=traffic_gb)
        await self._mirror(cl, "выдача", lambda p, o: _noop())
        return await self._enrich(cl)

    async def extend_client(self, *, client: Client, add_days: int,
                            set_total_gb: Optional[int] = None, add_total_gb: int = 0,
                            reset_traffic: bool = False) -> Client:
        cl = await self.primary.extend_client(client=client, add_days=add_days, set_total_gb=set_total_gb,
                                              add_total_gb=add_total_gb, reset_traffic=reset_traffic)

        async def op(p, o):
            # Сроки во вторичной выравниваем по главной, а не прибавляем вслепую —
            # иначе при рассинхроне они разъедутся ещё сильнее.
            delta_days = round(((cl.expire_ts or 0) - (o.expire_ts or 0)) / 86400) if cl.expire_ts else 0
            await p.extend_client(client=o, add_days=delta_days,
                                  set_total_gb=(cl.limit_bytes // 1024 ** 3) if cl.limit_bytes else None,
                                  reset_traffic=reset_traffic)
        await self._mirror(cl, "продление", op)
        return await self._enrich(cl)

    async def set_enabled(self, *, client: Client, enabled: bool) -> None:
        await self.primary.set_enabled(client=client, enabled=enabled)
        await self._mirror(client, "вкл/выкл", lambda p, o: p.set_enabled(client=o, enabled=enabled))

    async def set_unlimited(self, *, client: Client, reset_traffic: bool = True) -> Client:
        cl = await self.primary.set_unlimited(client=client, reset_traffic=reset_traffic)
        await self._mirror(cl, "безлимит", lambda p, o: p.set_unlimited(client=o, reset_traffic=reset_traffic))
        return await self._enrich(cl)

    async def delete_client(self, username: str) -> None:
        await self.primary.delete_client(username)
        for name, p in self.secondaries:
            try:
                if await p.find_by_username(username):
                    await p.delete_client(username)
            except Exception as e:  # noqa: BLE001
                self._warn(name, f"удаление {username}", e)

    async def reset_traffic(self, *, client: Client) -> None:
        await self.primary.reset_traffic(client=client)
        await self._mirror(client, "сброс трафика", lambda p, o: p.reset_traffic(client=o))

    async def bind_tgid(self, *, client: Client, tg_id: int) -> None:
        await self.primary.bind_tgid(client=client, tg_id=tg_id)
        await self._mirror(client, "привязка tg", lambda p, o: p.bind_tgid(client=o, tg_id=tg_id))


async def _noop() -> None:
    return None


def _tg_from_username(username: str) -> int:
    if username.startswith("u") and username[1:].isdigit():
        return int(username[1:])
    return 0
