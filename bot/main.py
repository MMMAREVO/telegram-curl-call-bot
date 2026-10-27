import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand

from . import config
from .admin_handlers import router as admin_router
from .handlers import router


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Регистрация / главное меню"),
        BotCommand(command="export", description="Выгрузить отчёт по сценарию"),
        BotCommand(command="new_campaign", description="Начать новый раунд тестирования (админ)"),
    ])


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    session = AiohttpSession(proxy=config.BOT_PROXY) if config.BOT_PROXY else None
    bot = Bot(token=config.BOT_TOKEN, session=session)
    await _set_commands(bot)
    dp = Dispatcher()
    # Админка — внутри основного router, чтобы её тоже проверял AccessMiddleware
    router.include_router(admin_router)
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
