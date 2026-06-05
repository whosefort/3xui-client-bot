"""Точка входа. Long-polling (без входящих портов) — минимальная поверхность атаки."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from . import db, runtime
from .config import config
from .handlers import setup_routers
from .scheduler import setup_scheduler
from .xui import XUIClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def main() -> None:
    config.validate()
    db.init(config.db_path)

    runtime.xui = XUIClient(
        config.xui_base_url,
        auth=config.xui_auth,
        api_token=config.xui_api_token,
        username=config.xui_username,
        password=config.xui_password,
        twofa_secret=config.xui_2fa_secret,
        client_flow=config.client_flow,
        verify_ssl=True,
    )

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runtime.bot = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup_routers())

    sched = setup_scheduler()
    sched.start()
    log.info("Планировщик запущен (напоминания в %02d:00 МСК)", config.remind_hour)

    try:
        log.info("Бот стартует (long-polling)…")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        sched.shutdown(wait=False)
        await runtime.xui.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено.")
