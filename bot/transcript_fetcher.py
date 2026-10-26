import asyncio
import datetime as dt
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import asyncpg

from . import config


@dataclass
class CallOutcome:
    completed: bool  # True, если в БД появилась запись с заполненным attempt_result
    call_id: Optional[str]  # call_id из БД — им именуются файлы лога/транскрипции
    attempt_result: Optional[str]
    transcribed: bool
    transcript: Optional[str]
    rec_file: Optional[str] = None  # ссылка на аудиозапись звонка (iba-wav-storage)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        database=config.PG_DB,
        user=config.PG_USER,
        password=config.PG_PASSWORD,
    )


async def _fetch_attempt_row(conn: asyncpg.Connection, phone: str, campaign_id: str, since: dt.datetime):
    query = (
        f"SELECT call_id, attempt_result, is_transcribed, rec_file, {config.PG_TEXT_COLUMN} AS transcript "
        f"FROM {config.PG_TABLE} "
        f"WHERE msisdn = $1 AND campaign_id = $2 AND start_datetime >= $3 "
        f"ORDER BY start_datetime DESC LIMIT 1"
    )
    return await conn.fetchrow(query, phone, uuid.UUID(campaign_id), since)


async def wait_for_call_outcome(
    phone: str,
    campaign_id: str,
    since: dt.datetime,
    on_tick: Optional[Callable[[float], Awaitable[None]]] = None,
) -> CallOutcome:
    """Опрашивает call_attempts, пока звонок не завершится (attempt_result заполнен),
    а затем — пока не появится транскрипция (is_transcribed = true), в пределах таймаутов.

    on_tick(elapsed_seconds), если задан, вызывается перед каждым ожиданием — удобно
    для отправки в чат промежуточных "всё ещё жду" сообщений.
    """
    if not config.PG_HOST or not config.PG_TABLE:
        raise RuntimeError("подключение к PostgreSQL не настроено (см. PG_* в .env)")

    start = time.monotonic()
    conn = await _connect()
    try:
        deadline = start + config.CALL_COMPLETION_TIMEOUT
        row = None
        while True:
            row = await _fetch_attempt_row(conn, phone, campaign_id, since)
            if row is not None and row["attempt_result"] is not None:
                break
            if time.monotonic() >= deadline:
                call_id = str(row["call_id"]) if row is not None and row["call_id"] is not None else None
                return CallOutcome(completed=False, call_id=call_id, attempt_result=None, transcribed=False, transcript=None)
            if on_tick:
                await on_tick(time.monotonic() - start)
            await asyncio.sleep(config.CALL_POLL_INTERVAL)

        call_id = str(row["call_id"]) if row["call_id"] is not None else None

        if row["is_transcribed"]:
            return CallOutcome(True, call_id, row["attempt_result"], True, row["transcript"], row["rec_file"])

        deadline = time.monotonic() + config.TRANSCRIPT_TIMEOUT
        while True:
            row = await _fetch_attempt_row(conn, phone, campaign_id, since)
            if row["is_transcribed"]:
                return CallOutcome(True, call_id, row["attempt_result"], True, row["transcript"], row["rec_file"])
            if time.monotonic() >= deadline:
                return CallOutcome(True, call_id, row["attempt_result"], False, None, row["rec_file"])
            if on_tick:
                await on_tick(time.monotonic() - start)
            await asyncio.sleep(config.CALL_POLL_INTERVAL)
    finally:
        await conn.close()
