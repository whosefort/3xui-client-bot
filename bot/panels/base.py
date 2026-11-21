"""Абстракция панели.

Хендлеры бота работают только с нормализованным `Client` и интерфейсом
`PanelClient`, не зная, что под капотом — 3x-ui или Marzban. Конкретный бэкенд
выбирается в рантайме по конфигу (PANEL_BACKEND).
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit


class PanelError(Exception):
    pass


@dataclass
class Client:
    """Нормализованный клиент — единая форма для всех бэкендов."""
    username: str                       # идентификатор (в 3x-ui это email); формат u{tg_id}
    tg_id: int = 0                      # 0 = не привязан
    sub_url: str = ""                   # готовая ссылка-подписка
    enabled: bool = True                # активен ли (не выключен админом)
    expire_ts: int = 0                  # unix-секунды; 0 = бессрочно
    used_bytes: int = 0
    limit_bytes: int = 0                # 0 = безлимит
    raw: dict = field(default_factory=dict)  # сырой объект бэкенда (для отладки)
    # Ссылки вторичных панелей (PANELS из нескольких) — клиент получает все.
    extra_subs: list = field(default_factory=list)
    private_access: bool | None = None
    panel_status: str | None = None

    @property
    def all_subs(self) -> list:
        return [u for u in [self.sub_url, *self.extra_subs] if u]

    def matches_sub_id(self, sub_id: str) -> bool:
        return bool(self.sub_url and sub_id
                    and urlsplit(self.sub_url).path.rstrip("/").rsplit("/", 1)[-1] == sub_id)

    @property
    def days_left(self) -> Optional[int]:
        if self.expire_ts <= 0:
            return None                 # бессрочно
        return max(0, int((self.expire_ts - time.time()) // 86400))

    @property
    def exhausted(self) -> bool:
        return self.limit_bytes > 0 and self.used_bytes >= self.limit_bytes

    @property
    def access_status(self) -> str:
        if self.panel_status == "disabled":
            return "paused"
        if self.panel_status == "expired" or 0 < self.expire_ts <= time.time():
            return "expired"
        if self.panel_status == "limited" or self.exhausted:
            return "limited"
        if self.panel_status == "on_hold":
            return "on_hold"
        return "active" if self.enabled else "paused"


class PanelClient(ABC):
    """Единый интерфейс для подписок разных панелей."""

    @staticmethod
    def username_for(tg_id: int) -> str:
        return f"u{int(tg_id)}"

    @abstractmethod
    async def close(self) -> None: ...

    # ---- чтение ----
    @abstractmethod
    async def list_clients(self, *, force: bool = False) -> list[Client]: ...

    @abstractmethod
    async def find_by_username(self, username: str) -> Optional[Client]: ...

    @abstractmethod
    async def find_by_tgid(self, tg_id: int) -> Optional[Client]: ...

    @abstractmethod
    async def find_by_subid(self, needle: str) -> Optional[Client]:
        """Найти клиента по хвосту его sub-ссылки (subId / sub-токен)."""

    # ---- запись ----
    @abstractmethod
    async def create_client(self, *, tg_id: int, days: int, traffic_gb: int,
                            username: str | None = None) -> Client: ...

    @abstractmethod
    async def extend_client(self, *, client: Client, add_days: int,
                            set_total_gb: Optional[int] = None, add_total_gb: int = 0,
                            reset_traffic: bool = False, expires_at: int | None = None) -> Client:
        """Сдвинуть срок (add_days может быть отрицательным — коррекция вниз).
        Лимит: set_total_gb — жёстко; add_total_gb — прибавить; иначе не трогаем.
        reset_traffic — обнулить счётчик. expires_at задаёт абсолютный срок
        для повтора продления и не сокращает срок, если тот уже больше."""

    @abstractmethod
    async def set_enabled(self, *, client: Client, enabled: bool) -> None: ...

    @abstractmethod
    async def set_unlimited(self, *, client: Client, reset_traffic: bool = True) -> Client:
        """Снять ограничения совсем: бессрочно (expire=0) + безлимитный
        трафик (limit=0). Отдельно от extend_client — та работает только
        относительными сдвигами (add_days), явного «сделать бессрочным» нет."""

    @abstractmethod
    async def delete_client(self, username: str) -> None: ...

    @abstractmethod
    async def reset_traffic(self, *, client: Client) -> None: ...

    @abstractmethod
    async def bind_tgid(self, *, client: Client, tg_id: int) -> None:
        """Привязать клиента к tg_id на стороне панели (для наглядности/поиска).
        Основная связка всё равно в bot.db."""
