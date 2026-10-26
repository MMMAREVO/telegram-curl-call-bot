from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class CallResult:
    ok: bool
    sid: Optional[str] = None
    raw: Optional[dict] = None
    error: Optional[str] = None


async def trigger_call(base_url: str, campaign_id: str, phone: str, params: dict) -> CallResult:
    url = f"{base_url}/campaigns/{campaign_id}/calls"
    payload = {"number": phone, "params": params}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        return CallResult(ok=False, error=f"не удалось выполнить запрос: {exc}")

    if resp.status_code // 100 != 2:
        return CallResult(ok=False, error=f"HTTP {resp.status_code}: {resp.text}")

    try:
        data = resp.json()
    except ValueError:
        return CallResult(ok=False, error=f"не удалось разобрать ответ сервера: {resp.text}")

    if data.get("status") != "ok":
        return CallResult(ok=False, raw=data, error=f"сервер вернул ошибку: {data}")

    sid = (data.get("data") or {}).get("sid")
    return CallResult(ok=True, sid=sid, raw=data)
