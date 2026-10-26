import datetime as dt
import json
import re
from dataclasses import dataclass

import httpx

from . import config

# Поля, которые забираем из VictoriaLogs: остальное (метки k8s и т.п.) только раздувает ответ
_FIELDS = "_time, _msg, app, kubernetes.pod_name"
_QUERY_LIMIT = 50000

# Строка asr-orch: "info - 2026-10-19T12:35:16.744150+00:00 - <session-id> ..." — по session-id
# находятся строки сессии без call_id (конец речи, распознанный текст).
_ASR_SESSION_RE = re.compile(r"^\w+ - \S+ - ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) ")
# sfas пишет id канала FreeSWITCH — по нему находятся строки playback и логи freeswitch
_FS_CALL_ID_RE = re.compile(r'"fs_call_id":"([0-9a-f-]{36})"')
# Суффиксы реплик deployment'а: grpc-vad-grpc-vad-service-774d5fb797-fpv9k -> grpc-vad-grpc-vad-service
_POD_HASH_RE = re.compile(r"-[0-9a-f]{8,10}-[0-9a-z]{5}$")


@dataclass
class VLogLine:
    time: str  # _time из VictoriaLogs (UTC, ISO)
    service: str  # под или systemd-сервис, откуда строка
    msg: str


def _service_name(rec: dict) -> str:
    pod = rec.get("kubernetes.pod_name") or ""
    if pod:
        return _POD_HASH_RE.sub("", pod)
    app = rec.get("app") or "unknown"
    return app.removesuffix(".service")


def _time_filter(start: dt.datetime, end: dt.datetime) -> str:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return f"_time:[{start.astimezone(dt.timezone.utc).strftime(fmt)}, {end.astimezone(dt.timezone.utc).strftime(fmt)}]"


async def _query(client: httpx.AsyncClient, ids: list[str], start: dt.datetime, end: dt.datetime) -> list[dict]:
    words = " OR ".join(f'"{i}"' for i in ids)
    # observability — сами VictoriaLogs/VictoriaMetrics: они логируют наши же запросы с call_id
    query = (
        f'{_time_filter(start, end)} ({words}) NOT kubernetes.pod_namespace:"observability" '
        f"| fields {_FIELDS} | sort by (_time) | limit {_QUERY_LIMIT}"
    )
    resp = await client.post(f"{config.VLOGS_URL}/select/logsql/query", data={"query": query})
    resp.raise_for_status()
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


async def fetch_call_logs(call_ids: list[str], start: dt.datetime, end: dt.datetime) -> list[VLogLine]:
    """Все строки логов звонка из VictoriaLogs, по времени.

    Первый запрос — по call_id/sid. Часть строк их не содержит (сессии ASR, playback во
    FreeSWITCH), поэтому из найденного достаём id сессий asr-orch и fs_call_id и
    делаем второй запрос по ним."""
    ids = [i for i in dict.fromkeys(call_ids) if i]
    if not ids:
        return []
    async with httpx.AsyncClient(timeout=60) as client:
        records = await _query(client, ids, start, end)

        extra: set[str] = set()
        for rec in records:
            msg = rec.get("_msg") or ""
            if _service_name(rec) == "cpaas-asr-orch-0":
                m = _ASR_SESSION_RE.match(msg)
                if m:
                    extra.add(m.group(1))
            extra.update(_FS_CALL_ID_RE.findall(msg))
        extra -= set(ids)
        if extra:
            records += await _query(client, sorted(extra), start, end)

    seen = set()
    lines = []
    for rec in sorted(records, key=lambda r: r.get("_time") or ""):
        key = (rec.get("_time"), rec.get("_msg"))
        if key in seen:
            continue
        seen.add(key)
        lines.append(VLogLine(time=rec.get("_time") or "", service=_service_name(rec), msg=rec.get("_msg") or ""))
    return lines


def group_by_service(lines: list[VLogLine]) -> dict[str, str]:
    """service -> текст лога (строки "время  сообщение"), для сохранения по файлам."""
    grouped: dict[str, list[str]] = {}
    for line in lines:
        grouped.setdefault(line.service, []).append(f"{line.time}  {line.msg}")
    return {service: "\n".join(rows) for service, rows in grouped.items()}
