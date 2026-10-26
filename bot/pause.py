import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Optional

from .latency import LatencyStats, compute_stats
from .vlogs_fetcher import VLogLine

# "info - 2026-10-19T12:35:16.744150+00:00 - <session> +0ms cpaas_asr_orc_session:init/1:123 start (gather), ..."
_ASR_LINE_RE = re.compile(r"^\w+ - (\S+) - ([0-9a-f-]{36}) (.*)$")
_HARD_ENDPOINT_RE = re.compile(r"VAD hard endpoint at \d+ ms \(speech end (\d+) ms\)")
_SPEECH_END_RE = re.compile(r"speech end (\d+) ms")
_FINALIZE_RE = re.compile(r"finalize/\d+:\d+ done \((\w+)\)")
_FRACTION_RE = re.compile(r"\.(\d+)")

# Команды sfas, которыми бот начинает говорить (фоновый звук start_background_play — не ответ)
_SPEAKING_COMMANDS = ("play", "say")


def parse_ts(value: str) -> dt.datetime:
    # В логах до 9 знаков после запятой, fromisoformat понимает максимум 6
    value = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ms(a: dt.datetime, b: dt.datetime) -> int:
    return round((b - a).total_seconds() * 1000)


@dataclass
class _AsrSession:
    session_id: str
    start: Optional[dt.datetime] = None
    endpoint_at: Optional[dt.datetime] = None  # VAD решил, что человек договорил
    speech_end_ms: Optional[int] = None  # конец речи от начала прослушивания
    finalized_at: Optional[dt.datetime] = None  # результат распознавания отдан в sfas
    finalize_reason: str = ""


@dataclass
class TurnPause:
    number: int
    session_id: str
    speech_end: dt.datetime  # человек замолчал
    bot_start: dt.datetime  # sfas начал проигрывать следующую фразу
    pause_ms: int
    detect_ms: Optional[int]  # конец речи -> VAD hard endpoint
    recognize_ms: Optional[int]  # endpoint -> результат ASR отдан
    processing_ms: int  # результат ASR -> начало следующей фразы (sengine, сценарий, классификатор)


def _asr_sessions(lines: list[VLogLine]) -> list[_AsrSession]:
    sessions: dict[str, _AsrSession] = {}
    for line in lines:
        if line.service != "cpaas-asr-orch-0":
            continue
        m = _ASR_LINE_RE.match(line.msg)
        if not m:
            continue
        ts, sid, rest = parse_ts(m.group(1)), m.group(2), m.group(3)
        s = sessions.setdefault(sid, _AsrSession(sid))
        if "start (gather)" in rest:
            s.start = ts
        hard = _HARD_ENDPOINT_RE.search(rest)
        if hard and s.endpoint_at is None:
            s.endpoint_at, s.speech_end_ms = ts, int(hard.group(1))
        elif s.endpoint_at is None:
            # Сессия может закончиться без hard endpoint (например, по общему таймауту) —
            # тогда берём последний известный конец речи
            for value in _SPEECH_END_RE.findall(rest):
                s.speech_end_ms = int(value)
        fin = _FINALIZE_RE.search(rest)
        if fin:
            s.finalized_at, s.finalize_reason = ts, fin.group(1)
    return sorted((s for s in sessions.values() if s.start), key=lambda s: s.start)


def bot_speech_starts(lines: list[VLogLine]) -> list[dt.datetime]:
    starts = []
    for line in lines:
        if line.service != "sfas_aas" or '"execute_se_command"' not in line.msg:
            continue
        try:
            data = json.loads(line.msg)
        except ValueError:
            continue
        if (data.get("text_data") or {}).get("command_type") in _SPEAKING_COMMANDS:
            starts.append(parse_ts(data["meta"]["time"]))
    return sorted(starts)


def extract_turn_pauses(lines: list[VLogLine]) -> list[TurnPause]:
    """Пауза для собеседника по каждому ходу: от конца его речи (asr-orch) до начала
    следующей фразы бота (sfas, команда play). Ходы без речи (first_speech_timeout) и
    ход, после которого бот уже ничего не сказал, не считаются."""
    plays = bot_speech_starts(lines)
    turns: list[TurnPause] = []
    for s in _asr_sessions(lines):
        if s.speech_end_ms is None or s.finalized_at is None or s.finalize_reason == "first_speech_timeout":
            continue
        speech_end = s.start + dt.timedelta(milliseconds=s.speech_end_ms)
        bot_start = next((p for p in plays if p >= s.finalized_at), None)
        if bot_start is None:
            continue
        turns.append(TurnPause(
            number=len(turns) + 1,
            session_id=s.session_id,
            speech_end=speech_end,
            bot_start=bot_start,
            pause_ms=_ms(speech_end, bot_start),
            detect_ms=_ms(speech_end, s.endpoint_at) if s.endpoint_at else None,
            recognize_ms=_ms(s.endpoint_at, s.finalized_at) if s.endpoint_at else None,
            processing_ms=_ms(s.finalized_at, bot_start),
        ))
    return turns



def pause_stats(turns: list[TurnPause]) -> Optional[LatencyStats]:
    return compute_stats([t.pause_ms for t in turns])
