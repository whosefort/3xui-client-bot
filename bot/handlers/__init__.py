from __future__ import annotations

from aiogram import Router

from . import admin, user


def setup_routers() -> Router:
    root = Router()
    # Админ-роутер первым: его фильтр по allowlist перехватывает админ-колбэки.
    root.include_router(admin.router)
    root.include_router(user.router)
    return root
