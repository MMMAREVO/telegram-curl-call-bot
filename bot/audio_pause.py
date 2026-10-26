import array
import datetime as dt
import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .pause import TurnPause, bot_speech_starts, parse_ts
from .vlogs_fetcher import VLogLine

FRAME_MS = 10
# Конец речи человека: громкость выше порога (с запасом над шумом канала)
_USER_MIN_THRESHOLD = 150
_USER_NOISE_FACTOR = 10
# Первый звук бота (вздох в начале фразы тоже считается): 3 кадра подряд выше порога
_BOT_MIN_THRESHOLD = 60
_BOT_NOISE_FACTOR = 3
_BOT_RUN_FRAMES = 3
# Где искать конец речи вокруг speech end из asr-orch, с
_USER_SEARCH_BEFORE_S, _USER_SEARCH_AFTER_S = 1.0, 0.6
# Если по записи вышло иначе, чем по логам, больше чем на столько — значит, зацепились
# не за то (шум, перебивание), и ход не считаем
_MAX_DIFF_FROM_LOG_MS = 1000


@dataclass
class HeardTurn:
    number: int  # тот же номер хода, что в логах
    pause_ms: Optional[int]  # человек замолчал -> первый звук бота, по записи
    note: str = ""  # почему не посчитано


def _channel_frames(path: Path) -> list[list[float]]:
    with wave.open(str(path)) as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"ожидался 16-битный WAV, а в файле {width * 8} бит")
    samples = array.array("h", raw)
    step = rate * FRAME_MS // 1000
    result = []
    for c in range(channels):
        ch = samples[c::channels]
        result.append([
            math.sqrt(sum(v * v for v in ch[i:i + step]) / len(ch[i:i + step]))
            for i in range(0, len(ch) - step + 1, step)
        ])
    return result


def _noise_floor(frames: list[float]) -> float:
    return sorted(frames)[len(frames) // 5]


def _record_start(lines: list[VLogLine]) -> Optional[dt.datetime]:
    """Момент, когда FreeSWITCH начал запись (первое событие record_start в sfas)."""
    for line in lines:
        if line.service == "sfas_aas" and "handle_fs_event" in line.msg and "record_start" in line.msg:
            try:
                return parse_ts(json.loads(line.msg)["meta"]["time"])
            except (ValueError, KeyError):
                continue
    return None


def _first_run(frames: list[float], start: int, end: int, thr: float, run: int) -> Optional[int]:
    for i in range(max(0, start), min(len(frames) - run, end)):
        if all(frames[j] >= thr for j in range(i, i + run)):
            return i
    return None


def _to_frame(seconds: float) -> int:
    return round(seconds * 1000 / FRAME_MS)


def measure_heard_pauses(wav_path: Path, log_turns: list[TurnPause], lines: list[VLogLine]) -> tuple[list[HeardTurn], str]:
    """Пауза "как слышится": по стерео-записи от конца речи человека до первого звука бота.

    Логи нужны только чтобы найти ходы и совместить время записи с временем логов —
    сама пауза меряется по звуку. Возвращает (ходы, причина, если посчитать нельзя)."""
    if not log_turns:
        return [], "в логах нет ходов с ответом"
    rec_start = _record_start(lines)
    plays = bot_speech_starts(lines)
    if rec_start is None or not plays:
        return [], "в логах sfas нет начала записи или команд play"

    frames = _channel_frames(wav_path)
    if len(frames) != 2:
        return [], f"запись не стерео ({len(frames)} канал(а)) — голоса не разделить"
    floors = [_noise_floor(f) for f in frames]
    bot_thr = [max(_BOT_MIN_THRESHOLD, _BOT_NOISE_FACTOR * fl) for fl in floors]

    # Канал бота — тот, где звучит фраза в течение секунды после команды play
    def activity_after_plays(c: int) -> float:
        hits = total = 0
        for p in plays:
            a = _to_frame((p - rec_start).total_seconds())
            window = frames[c][a + 30:a + 130]
            hits += sum(1 for v in window if v >= bot_thr[c])
            total += len(window)
        return hits / total if total else 0.0

    bot_ch = max((0, 1), key=activity_after_plays)
    user_ch = 1 - bot_ch
    bot, user = frames[bot_ch], frames[user_ch]
    b_thr = bot_thr[bot_ch]
    u_thr = max(_USER_MIN_THRESHOLD, _USER_NOISE_FACTOR * floors[user_ch])

    # Уточняем, какому моменту логов соответствует начало записи: звук бота не может
    # появиться раньше команды play, а самая быстрая фраза стартует почти сразу после неё
    deltas = []
    for p in plays:
        a = _to_frame((p - rec_start).total_seconds())
        onset = _first_run(bot, a - 60, a + 100, b_thr, _BOT_RUN_FRAMES)
        if onset is not None and onset >= 15 and max(bot[onset - 15:onset]) < b_thr:
            deltas.append(onset - a)
    shift_frames = min(deltas) if deltas else 0
    audio_zero = rec_start - dt.timedelta(milliseconds=shift_frames * FRAME_MS)

    result = []
    for t in log_turns:
        e = _to_frame((t.speech_end - audio_zero).total_seconds())
        lo, hi = e - _to_frame(_USER_SEARCH_BEFORE_S), e + _to_frame(_USER_SEARCH_AFTER_S)
        loud = [i for i in range(max(0, lo), min(len(user), hi)) if user[i] >= u_thr]
        if not loud:
            result.append(HeardTurn(t.number, None, "речь в записи не найдена"))
            continue
        user_end = loud[-1] + 1
        if any(v >= b_thr for v in bot[max(0, user_end - 5):user_end]):
            result.append(HeardTurn(t.number, None, "бот говорил одновременно с человеком"))
            continue
        onset = _first_run(bot, user_end, user_end + 600, b_thr, _BOT_RUN_FRAMES)
        if onset is None:
            result.append(HeardTurn(t.number, None, "ответ бота в записи не найден"))
            continue
        pause_ms = (onset - user_end) * FRAME_MS
        if abs(pause_ms - t.pause_ms) > _MAX_DIFF_FROM_LOG_MS:
            result.append(HeardTurn(t.number, None, f"по записи {pause_ms} мс — слишком далеко от логов, не считаю"))
            continue
        result.append(HeardTurn(t.number, pause_ms))
    return result, ""
