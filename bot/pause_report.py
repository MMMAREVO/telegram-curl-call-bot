import statistics
from typing import Optional

from .audio_pause import HeardTurn
from .latency import compute_stats, fmt_ms, format_stats
from .pause import TurnPause, pause_stats


def _avg(values: list[Optional[int]]) -> Optional[float]:
    known = [v for v in values if v is not None]
    return statistics.fmean(known) if known else None


def _stage_line(title: str, value: Optional[float]) -> str:
    return f" • {title}: {fmt_ms(value) if value is not None else 'нет данных'}"


def heard_values(heard: list[HeardTurn]) -> list[int]:
    return [h.pause_ms for h in heard if h.pause_ms is not None]


def _heard_block(heard: list[HeardTurn], heard_error: str) -> list[str]:
    lines = ["🎧 Как слышится (по записи, до первого звука бота):"]
    stats = compute_stats(heard_values(heard))
    if stats is None:
        return lines + [f"нет данных — {heard_error or 'ни один ход не удалось измерить по записи'}"]
    lines.append(format_stats(stats))
    slowest = max((h for h in heard if h.pause_ms is not None), key=lambda h: h.pause_ms)
    lines.append(f"Самый медленный ход: №{slowest.number} — {fmt_ms(slowest.pause_ms)}")
    skipped = [h for h in heard if h.pause_ms is None]
    if skipped:
        lines.append("Не посчитаны: " + "; ".join(f"№{h.number} — {h.note}" for h in skipped))
    return lines


def _log_block(turns: list[TurnPause]) -> list[str]:
    lines = ["📄 По логам (конец речи по asr-orch → команда play в sfas):"]
    stats = pause_stats(turns)
    if stats is None:
        return lines + ["нет данных — в логах VictoriaLogs не найдено ходов с ответом"]
    slowest = max(turns, key=lambda t: t.pause_ms)
    return lines + [
        format_stats(stats),
        f"Самый медленный ход: №{slowest.number} — {fmt_ms(slowest.pause_ms)}",
        "В среднем по этапам:",
        _stage_line("понять, что человек договорил (VAD)", _avg([t.detect_ms for t in turns])),
        _stage_line("распознать текст (ASR)", _avg([t.recognize_ms for t in turns])),
        _stage_line("обработка до начала фразы", _avg([t.processing_ms for t in turns])),
    ]


def format_call_pause(turns: list[TurnPause], heard: list[HeardTurn], heard_error: str) -> str:
    return "\n".join(
        ["⏱ Пауза: человек замолчал → бот ответил", ""]
        + _heard_block(heard, heard_error) + [""] + _log_block(turns)
    )


def format_timing_report(turns: list[TurnPause], heard: list[HeardTurn], heard_error: str) -> str:
    """Подробный отчёт по ходам — кладётся файлом в архив с логами."""
    def cell(v: Optional[int]) -> str:
        return "-" if v is None else str(v)

    by_number = {h.number: h for h in heard}
    rows = [
        "Пауза по ходам, мс. Время — UTC.",
        "Как слышится — по стерео-записи: последний звук речи человека -> первый звук бота (вздох тоже считается).",
        "По логам — start (gather) + speech end из asr-orch -> execute_se_command play в sfas.",
        "",
        "№\tкак слышится\tпо логам\tVAD\tASR\tобработка\tзамолчал (лог)\tбот заговорил (лог)\tсессия ASR\tзаметка",
    ]
    for t in turns:
        h = by_number.get(t.number)
        rows.append("\t".join([
            str(t.number), cell(h.pause_ms if h else None), str(t.pause_ms),
            cell(t.detect_ms), cell(t.recognize_ms), str(t.processing_ms),
            t.speech_end.strftime("%H:%M:%S.%f")[:-3], t.bot_start.strftime("%H:%M:%S.%f")[:-3],
            t.session_id, h.note if h else "",
        ]))
    return "\n".join(rows + ["", format_call_pause(turns, heard, heard_error)])
