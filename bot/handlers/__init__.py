from __future__ import annotations

from aiogram import Router

from . import admin, private_billing, user, xui_admin, xui_infra


def setup_routers() -> Router:
    root = Router()
    # Админ-роутер первым: его фильтр по allowlist перехватывает админ-колбэки.
    root.include_router(xui_infra.router)
    root.include_router(xui_admin.router)
    root.include_router(private_billing.router)
    root.include_router(admin.router)
    root.include_router(user.router)
    return root
