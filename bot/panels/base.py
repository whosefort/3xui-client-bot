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

    @property
    def days_left(self) -> Optional[int]:
        if self.expire_ts <= 0:
            return None                 # бессрочно
        return max(0, int((self.expire_ts - time.time()) // 86400))

    @property
    def exhausted(self) -> bool:
        return self.limit_bytes > 0 and self.used_bytes >= self.limit_bytes


class PanelClient(ABC):
    """Единый интерфейс панели. Все методы идемпотентны по username."""

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
    async def create_client(self, *, tg_id: int, days: int, traffic_gb: int) -> Client: ...

    @abstractmethod
    async def extend_client(self, *, client: Client, add_days: int,
                            set_total_gb: Optional[int] = None, add_total_gb: int = 0,
                            reset_traffic: bool = False) -> Client:
        """Сдвинуть срок (add_days может быть отрицательным — коррекция вниз).
        Лимит: set_total_gb — жёстко; add_total_gb — прибавить; иначе не трогаем.
        reset_traffic — обнулить счётчик."""

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
