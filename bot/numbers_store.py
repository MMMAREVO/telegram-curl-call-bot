import uuid
from dataclasses import dataclass

from . import campaigns, config
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
    return ", ".join(c.title for c in campaigns.all_campaigns() if c.campaign_id == campaign_id)


async def _insert_numbers(phones: list[str], campaign_ids: list[str]) -> list[tuple[str, str]]:
    """Добавляет номера в кампании (iba.numbers). Уже существующие строки не трогает.
    Возвращает добавленные пары (номер, campaign_id)."""
    if not config.PG_HOST:
        raise RuntimeError("подключение к PostgreSQL не настроено (см. PG_* в .env)")
    conn = await _connect()
    try:
        rows = await conn.fetch(
            "INSERT INTO iba.numbers (tenant_id, msisdn, campaign_id, priority, status, params) "
            "SELECT c.tenant_id, p.msisdn, c.id, $3, $4, '{}'::jsonb "
            "FROM iba.campaigns c CROSS JOIN unnest($1::text[]) AS p(msisdn) "
            "WHERE c.id = ANY($2::uuid[]) "
            "ON CONFLICT (tenant_id, msisdn, campaign_id) DO NOTHING "
            "RETURNING msisdn, campaign_id",
            phones, [uuid.UUID(x) for x in campaign_ids], NUMBER_PRIORITY, NUMBER_STATUS,
        )
    finally:
        await conn.close()
    return [(r["msisdn"], str(r["campaign_id"])) for r in rows]


async def add_number_to_campaigns(phone: str) -> AddNumberResult:
    """Добавляет номер во все кампании бота."""
    campaign_ids = sorted({c.campaign_id for c in campaigns.all_campaigns()})
    added_ids = {cid for _, cid in await _insert_numbers([phone], campaign_ids)}
    return AddNumberResult(
        added=[_campaign_titles(cid) for cid in campaign_ids if cid in added_ids],
        existing=[_campaign_titles(cid) for cid in campaign_ids if cid not in added_ids],
    )


async def add_numbers_to_campaign(phones: list[str], campaign_id: str) -> int:
    """Добавляет номера всех исполнителей в одну (новую) кампанию. Возвращает, сколько добавлено."""
    if not phones:
        return 0
    return len(await _insert_numbers(phones, [campaign_id]))
