import uuid
from dataclasses import dataclass

from . import config
from .campaigns import CAMPAIGNS
from .transcript_fetcher import _connect

# Номер заводится в iba.numbers уже "отработанным" (done), чтобы платформа сама
# не начала на него звонить — звонки по-прежнему запускаются только курлом из бота.
NUMBER_STATUS = "done"
NUMBER_PRIORITY = 99


@dataclass
class AddNumberResult:
    added: list[str]  # названия кампаний, куда номер добавлен сейчас
    existing: list[str]  # названия кампаний, где номер уже был


def _campaign_titles(campaign_id: str) -> str:
    return ", ".join(c.title for c in CAMPAIGNS.values() if c.campaign_id == campaign_id)


async def add_number_to_campaigns(phone: str) -> AddNumberResult:
    """Добавляет номер во все кампании бота (iba.numbers). Уже существующие строки не трогает."""
    if not config.PG_HOST:
        raise RuntimeError("подключение к PostgreSQL не настроено (см. PG_* в .env)")

    campaign_ids = sorted({c.campaign_id for c in CAMPAIGNS.values()})
    conn = await _connect()
    try:
        rows = await conn.fetch(
            "INSERT INTO iba.numbers (tenant_id, msisdn, campaign_id, priority, status, params) "
            "SELECT c.tenant_id, $1, c.id, $3, $4, '{}'::jsonb "
            "FROM iba.campaigns c WHERE c.id = ANY($2::uuid[]) "
            "ON CONFLICT (tenant_id, msisdn, campaign_id) DO NOTHING "
            "RETURNING campaign_id",
            phone, [uuid.UUID(x) for x in campaign_ids], NUMBER_PRIORITY, NUMBER_STATUS,
        )
    finally:
        await conn.close()

    added_ids = {str(r["campaign_id"]) for r in rows}
    return AddNumberResult(
        added=[_campaign_titles(cid) for cid in campaign_ids if cid in added_ids],
        existing=[_campaign_titles(cid) for cid in campaign_ids if cid not in added_ids],
    )
