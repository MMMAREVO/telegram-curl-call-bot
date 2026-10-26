import statistics
from dataclasses import dataclass
from typing import Optional


@dataclass
class LatencyStats:
    turns: int
    avg_ms: float
    median_ms: float
    min_ms: int
    max_ms: int


def compute_stats(latencies_ms: list[int]) -> Optional[LatencyStats]:
    if not latencies_ms:
        return None
    return LatencyStats(
        turns=len(latencies_ms),
        avg_ms=statistics.fmean(latencies_ms),
        median_ms=statistics.median(latencies_ms),
        min_ms=min(latencies_ms),
        max_ms=max(latencies_ms),
    )


def fmt_ms(ms: float) -> str:
    return f"{ms:.0f} мс ({ms / 1000:.2f} с)"


def format_stats(stats: LatencyStats) -> str:
    return "\n".join([
        f"Среднее: {fmt_ms(stats.avg_ms)}",
        f"Медиана: {fmt_ms(stats.median_ms)}",
        f"Минимум: {fmt_ms(stats.min_ms)}",
        f"Максимум: {fmt_ms(stats.max_ms)}",
        f"Ходов: {stats.turns}",
    ])


def turns_from_cell(value) -> list[int]:
    """Задержки ходов из ячейки Excel ("1455;1230;1329") — для сводки по кампании."""
    if not value:
        return []
    return [int(x) for x in str(value).split(";") if x.strip().lstrip("-").isdigit()]
