"""Общая обвязка тестов: изолированные файлы данных и подставной Telegram.

Бот гоняется через настоящий Dispatcher aiogram, а вместо Telegram Bot API — FakeSession,
которая записывает исходящие запросы и проверяет реальные ограничения Telegram
(длина сообщения и подписи, callback_data, число кнопок, размер файла, HTML-разметка).
Сеть, БД и SSH не трогаются: PG_HOST пустой, запуск звонка и сбор результатов подменены."""
import asyncio
import datetime as dt
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ADMIN_ID = 900000001  # админ из .env (ADMIN_TELEGRAM_IDS)
PASSWORD = "test-password"

# config читает окружение при импорте; load_dotenv не перезаписывает уже заданные переменные
os.environ.update({
    "BOT_TOKEN": "123456:TEST",
    "BOT_ACCESS_PASSWORD": PASSWORD,
    "ADMIN_TELEGRAM_IDS": str(ADMIN_ID),
    "CAMPAIGNS_API_BASE": "http://campaigns.test",
    "PG_HOST": "",
    "PG_TABLE": "",
    "VLOGS_URL": "",
    "BOT_PROXY": "",
    "HTTPS_PROXY": "",
    "HTTP_PROXY": "",
})

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User  # noqa: E402

from bot import access, campaigns, config, excel_store, handlers  # noqa: E402
from bot.admin_handlers import router as admin_router  # noqa: E402
from bot.api_client import CallResult  # noqa: E402

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
TG_CALLBACK_DATA_LIMIT = 64
TG_BUTTONS_LIMIT = 100
TG_UPLOAD_LIMIT = 50 * 1024 * 1024
_ALLOWED_HTML = re.compile(r"</?(b|i|u|s|code|pre)>")

VALID_CURL = (
    "curl -X POST 'http://campaigns.test/campaigns/11111111-2222-3333-4444-555555555555/calls' "
    "-H 'Content-Type: application/json' "
    "-d '{\"number\": \"998900000000\", \"params\": {\"lang\": \"ru\", \"brand\": \"{{brand}}\", \"color\": \"{{color}}\"}}'"
)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        text = getattr(method, "text", None)
        caption = getattr(method, "caption", None)
        markup = getattr(method, "reply_markup", None)
        buttons = []
        if markup is not None and hasattr(markup, "inline_keyboard"):
            buttons = [b for row in markup.inline_keyboard for b in row]
        document = getattr(method, "document", None)
        data = getattr(document, "data", None)
        record = {
            "method": name, "text": text, "caption": caption, "parse_mode": getattr(method, "parse_mode", None),
            "buttons": [b.callback_data for b in buttons], "button_texts": [b.text for b in buttons],
            "filename": getattr(document, "filename", None), "data": data, "timeout": timeout,
            "chat_id": getattr(method, "chat_id", None),
        }
        self.sent.append(record)

        if text is not None and len(text) > TG_TEXT_LIMIT:
            raise TelegramBadRequest(method=method, message="Bad Request: message is too long")
        if caption is not None and len(caption) > TG_CAPTION_LIMIT:
            raise TelegramBadRequest(method=method, message="Bad Request: message caption is too long")
        if len(buttons) > TG_BUTTONS_LIMIT:
            raise TelegramBadRequest(method=method, message="Bad Request: reply markup is too long")
        for b in buttons:
            if b.callback_data is not None and len(b.callback_data.encode("utf-8")) > TG_CALLBACK_DATA_LIMIT:
                raise TelegramBadRequest(method=method, message="Bad Request: BUTTON_DATA_INVALID")
        if data is not None and len(data) > TG_UPLOAD_LIMIT:
            raise TelegramEntityTooLarge(method=method, message="Request Entity Too Large")
        if record["parse_mode"] == "HTML" and "<" in _ALLOWED_HTML.sub("", text or caption or ""):
            raise TelegramBadRequest(method=method, message="Bad Request: can't parse entities")

        if method.__returning__ is bool:
            return True
        return Message(message_id=1, date=dt.datetime.now(), chat=Chat(id=record["chat_id"] or 1, type="private"))

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""


session = FakeSession()
bot = Bot("123456:TEST", session=session)
# Файлы, которые "присылает" пользователь: file_id -> содержимое (или исключение при скачивании)
uploaded_files: dict[str, bytes | Exception] = {}


async def _fake_download(file, destination=None, **kwargs):
    content = uploaded_files[file.file_id]
    if isinstance(content, Exception):
        raise content
    destination.write(content)
    destination.seek(0)
    return destination


bot.download = _fake_download
handlers.router.include_router(admin_router)
dp = Dispatcher()
dp.include_router(handlers.router)
_update_id = [0]


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """Каждый тест — в своей временной папке: свой Excel, campaigns.json, ротация, call_logs."""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bot_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for attr, value in {
            "EXCEL_PATH": str(self.tmp / "bot_data.xlsx"),
            "CAMPAIGNS_PATH": str(self.tmp / "campaigns.json"),
            "VAR_ROTATION_PATH": str(self.tmp / "var_rotation.json"),
            "CAR_MODELS_PATH": str(self.tmp / "model_marka.txt"),
            "LOGS_DIR": str(self.tmp / "call_logs"),
        }.items():
            old = getattr(config, attr)
            setattr(config, attr, value)
            self.addCleanup(setattr, config, attr, old)
        excel_store._lock = asyncio.Lock()
        campaigns._campaigns = None
        access._registered = None
        access._admins = set()
        access._failed.clear()
        handlers._active_calls.clear()
        dp.storage.storage.clear()
        uploaded_files.clear()
        session.sent.clear()

        # Звонок не уходит в сеть: запоминаем тело запроса, результат задаёт тест
        self.triggered: list[tuple[str, dict, dict]] = []
        self.call_result = CallResult(ok=False, error="тестовый режим: курл не отправлен")
        self.processed: list[str] = []

        async def fake_trigger(url, headers, body):
            self.triggered.append((url, headers, body))
            return self.call_result

        async def fake_process(message, state, campaign, phone, result, *args):
            self.processed.append(campaign.key)

        for target, fake in (("trigger_call", fake_trigger), ("_process_call_result", fake_process)):
            old = getattr(handlers, target)
            setattr(handlers, target, fake)
            self.addCleanup(setattr, handlers, target, old)

    # --- отправка событий в бота ---

    async def _feed(self, update: Update) -> list[dict]:
        start = len(session.sent)
        await dp.feed_update(bot, update)
        return session.sent[start:]

    async def send(self, user_id: int, text: str | None = None, document: tuple[str, bytes | Exception] | None = None):
        _update_id[0] += 1
        doc = None
        if document is not None:
            file_id = f"file{_update_id[0]}"
            uploaded_files[file_id] = document[1]
            doc = Document(file_id=file_id, file_unique_id=file_id, file_name=document[0])
        msg = Message(
            message_id=_update_id[0], date=dt.datetime.now(), chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"), text=text, document=doc,
        )
        return await self._feed(Update(update_id=_update_id[0], message=msg))

    async def press(self, user_id: int, data: str):
        _update_id[0] += 1
        msg = Message(
            message_id=_update_id[0], date=dt.datetime.now(), chat=Chat(id=user_id, type="private"),
            from_user=User(id=1, is_bot=True, first_name="bot"), text="меню",
        )
        query = CallbackQuery(
            id=str(_update_id[0]), from_user=User(id=user_id, is_bot=False, first_name="T"),
            chat_instance="c", message=msg, data=data,
        )
        return await self._feed(Update(update_id=_update_id[0], callback_query=query))

    async def no_crash(self, coro) -> list[dict]:
        """Событие должно обработаться без исключения: упавший обработчик = пользователь без ответа."""
        try:
            return await coro
        except Exception as exc:
            self.fail(f"обработчик упал с исключением: {type(exc).__name__}: {exc}")

    # --- подготовка данных ---

    async def register(self, user_id: int, name: str = "Тестер", phone: str = "998901234567") -> None:
        await excel_store.register_user(user_id, name, phone)
        access.reset_cache()

    async def add_campaign_via_bot(self, title: str = "Тестовая", curl: str = VALID_CURL) -> campaigns.Campaign:
        await self.press(ADMIN_ID, "adm:camp_add")
        await self.send(ADMIN_ID, curl)
        await self.send(ADMIN_ID, title)
        campaign = campaigns.by_title(title)
        assert campaign is not None, f"кампания «{title}» не добавилась"
        return campaign


def texts(out: list[dict]) -> list[str]:
    return [r["text"] or r["caption"] or "" for r in out if r["method"] in ("SendMessage", "SendDocument")]


def joined(out: list[dict]) -> str:
    return "\n".join(texts(out))


def documents(out: list[dict]) -> list[dict]:
    return [r for r in out if r["method"] == "SendDocument"]


def all_buttons(out: list[dict]) -> list[str]:
    return [b for r in out for b in r["buttons"]]
