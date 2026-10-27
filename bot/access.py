import hmac
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from . import config, excel_store

# Неверный пароль столько раз подряд — блокируем попытки на время, чтобы его не подбирали
MAX_PASSWORD_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60

_registered: set[int] | None = None  # кэш telegram_id из листа Users
_admins: set[int] = set()  # кэш админов, назначенных из бота (колонка is_admin листа Users)
_failed: dict[int, tuple[int, float]] = {}  # telegram_id -> (неудачных попыток подряд, заблокирован до)


async def _ensure_loaded() -> None:
    global _registered, _admins
    if _registered is None:
        users = await excel_store.list_users()
        _registered = {u["telegram_id"] for u in users}
        _admins = {u["telegram_id"] for u in users if u["is_admin"]}


def reset_cache() -> None:
    """Таблицу пользователей поменял админ — перечитать при следующем обращении."""
    global _registered
    _registered = None


def is_env_admin(telegram_id: int) -> bool:
    return telegram_id in config.ADMIN_TELEGRAM_IDS


def is_admin(telegram_id: int) -> bool:
    # Кэш _admins гарантированно загружен: любое событие сначала проходит AccessMiddleware
    return is_env_admin(telegram_id) or telegram_id in _admins


async def is_registered(telegram_id: int) -> bool:
    await _ensure_loaded()
    return telegram_id in _registered


def mark_registered(telegram_id: int) -> None:
    if _registered is not None:
        _registered.add(telegram_id)


def lockout_left(telegram_id: int) -> int:
    """Сколько секунд ещё действует блокировка после неудачных попыток (0 — не заблокирован)."""
    _, until = _failed.get(telegram_id, (0, 0.0))
    return max(0, int(until - time.monotonic()))


def check_password(telegram_id: int, password: str) -> bool:
    ok = hmac.compare_digest(password.encode(), config.ACCESS_PASSWORD.encode())
    if ok:
        _failed.pop(telegram_id, None)
        return True
    fails = _failed.get(telegram_id, (0, 0.0))[0] + 1
    until = time.monotonic() + LOCKOUT_SECONDS if fails >= MAX_PASSWORD_ATTEMPTS else 0.0
    _failed[telegram_id] = (0 if until else fails, until)
    return False


def attempts_left(telegram_id: int) -> int:
    return MAX_PASSWORD_ATTEMPTS - _failed.get(telegram_id, (0, 0.0))[0]


class AccessMiddleware(BaseMiddleware):
    """Пускает к боту только зарегистрированных пользователей и админов. Остальным
    доступны лишь /start и шаги регистрации (пароль -> имя -> номер)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        await _ensure_loaded()
        user = data.get("event_from_user")
        if user is not None and (is_admin(user.id) or await is_registered(user.id)):
            return await handler(event, data)
        if (data.get("raw_state") or "").startswith("Registration:"):
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа. Отправьте /start и введите пароль.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("Нет доступа. Отправьте /start и введите пароль.")
        return None
