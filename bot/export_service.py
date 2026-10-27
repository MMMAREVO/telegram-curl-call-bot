import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openpyxl import Workbook

from . import excel_store, latency

REPORT_COLUMNS = [
    "round", "timestamp", "telegram_id", "name", "phone", "campaign", "test_plan", "test_case",
    "status", "call_result", "log_file", "transcription", "comment", "call_id",
    "heard_avg_ms", "heard_median_ms", "heard_min_ms", "heard_max_ms",
    "pause_avg_ms", "pause_median_ms", "pause_min_ms", "pause_max_ms",
]


def build_report_bytes(rows: list[dict]) -> bytes:
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Report"
    sheet.append(REPORT_COLUMNS)
    for r in rows:
        excel_store.append_row(sheet, [r.get(col, "") for col in REPORT_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse_timestamp(value) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def build_stats_text(
    all_rows: list[dict], campaign_title: str, filter_label: str, scope_label: str
) -> str:
    """Статистика всегда считается по ПОЛНОМУ набору звонков за период (all_rows),
    а не по отфильтрованным 'только ошибки' строкам — иначе процент был бы бессмысленным."""
    total = len(all_rows)
    errors = sum(1 for r in all_rows if excel_store.is_error_row(r))
    success = total - errors
    error_pct = (errors / total * 100) if total else 0.0

    timestamps = [t for t in (_parse_timestamp(r.get("timestamp")) for r in all_rows) if t is not None]
    if timestamps:
        first = min(timestamps).strftime("%d.%m.%Y %H:%M")
        last = max(timestamps).strftime("%d.%m.%Y %H:%M")
        period_line = f"Период: {scope_label}, с {first} по {last}"
    else:
        period_line = f"Период: {scope_label}"

    lines = [
        f"Кампания: {campaign_title}",
        f"Фильтр: {filter_label}",
        period_line,
        "",
        f"Звонков всего: {total}",
        f"Звонков успешных: {success}",
        f"Звонков с ошибкой: {errors} ({error_pct:.1f}%)",
    ]

    # Сводки по всем ходам всех звонков, а не среднее из средних по звонкам
    for column, title in (
        ("heard_turns_ms", "🎧 Пауза как слышится (по записи, до первого звука бота):"),
        ("pause_turns_ms", "📄 Пауза по логам (asr-orch → play в sfas):"),
    ):
        all_turns = [ms for r in all_rows for ms in latency.turns_from_cell(r.get(column))]
        stats = latency.compute_stats(all_turns)
        if stats is not None:
            calls_measured = sum(1 for r in all_rows if r.get(column))
            lines += ["", title, latency.format_stats(stats), f"Звонков с замером: {calls_measured}"]
    return "\n".join(lines)


# Telegram не даёт боту отправить файл больше 50 МБ — режем выгрузку на части с запасом.
ARCHIVE_PART_LIMIT = 49 * 1024 * 1024
# Запас на заголовки zip для каждого файла
_ZIP_ENTRY_OVERHEAD = 1024

# (путь на диске или содержимое, путь внутри архива)
FileGroup = list[tuple[Path | bytes, str]]


def _collect_files(filtered_rows: list[dict], audio: bool) -> list[FileGroup]:
    """Файлы выбранных звонков, сгруппированные по звонку: логи и транскрипция
    (audio=False) либо аудиозапись (audio=True)."""
    groups: list[FileGroup] = []
    seen = set()

    for r in filtered_rows:
        group: FileGroup = []

        def add(path_str: str, folder: str) -> None:
            path_str = path_str.strip()
            if path_str and path_str not in seen and Path(path_str).exists():
                seen.add(path_str)
                group.append((Path(path_str), f"{folder}/{Path(path_str).name}"))

        if audio:
            add(r.get("audio_file") or "", "audio")
        else:
            for path_str in (r.get("log_file") or "").split("; "):
                add(path_str, "logs")
            add(r.get("transcript_file") or "", "transcripts")
        if group:
            groups.append(group)
    return groups


def _source_size(source: Path | bytes) -> int:
    return len(source) if isinstance(source, bytes) else source.stat().st_size


@dataclass
class ArchivePlan:
    """Раскладка файлов по частям архива. Части собираются по одной (build), чтобы
    большая выгрузка не лежала в памяти целиком."""
    parts: list[FileGroup]
    first_part_extra: dict[str, bytes]
    skipped: list[str]
    compression: int

    def build(self, index: int) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", self.compression) as zf:
            if index == 0:
                for name, content in self.first_part_extra.items():
                    zf.writestr(name, content)
                if self.skipped:
                    zf.writestr(
                        "skipped.txt",
                        "Файлы больше 49 МБ не поместились в архив (лежат в call_logs на компьютере с ботом):\n"
                        + "\n".join(self.skipped),
                    )
            for source, arcname in self.parts[index]:
                # wav почти не сжимается — не тратим на него время
                compress = zipfile.ZIP_STORED if arcname.lower().endswith(".wav") else self.compression
                if isinstance(source, bytes):
                    zf.writestr(arcname, source, compress_type=compress)
                else:
                    zf.write(source, arcname=arcname, compress_type=compress)
        return buf.getvalue()


def _plan_archives(
    groups: list[FileGroup], first_part_extra: dict[str, bytes], compression: int
) -> ArchivePlan | None:
    """Раскладывает файлы по zip-архивам не больше ARCHIVE_PART_LIMIT каждый.

    Размер части оценивается по несжатому размеру файлов — оценка с запасом. Файлы одного
    звонка кладутся в одну часть. Файл, который сам по себе больше лимита, в архив не
    попадает и перечисляется в skipped.txt первой части. first_part_extra — файлы
    (имя -> содержимое), которые нужно положить в первую часть, например stats.txt.
    None — класть в архив нечего."""
    max_file_size = ARCHIVE_PART_LIMIT - _ZIP_ENTRY_OVERHEAD * 4

    parts: list[FileGroup] = [[]]
    part_size = sum(len(b) + _ZIP_ENTRY_OVERHEAD for b in first_part_extra.values())
    skipped: list[str] = []
    for group in groups:
        sized = []
        for source, arcname in group:
            size = _source_size(source) + _ZIP_ENTRY_OVERHEAD
            if size > max_file_size:
                skipped.append(arcname)
            else:
                sized.append((source, arcname, size))
        group_size = sum(size for _, _, size in sized)
        # Звонок целиком не влезает даже в пустую часть — раскладываем его файлы по одному
        chunks = [sized] if group_size <= max_file_size else [[f] for f in sized]
        for chunk in chunks:
            chunk_size = sum(size for _, _, size in chunk)
            if part_size + chunk_size > ARCHIVE_PART_LIMIT and parts[-1]:
                parts.append([])
                part_size = 0
            parts[-1].extend((source, arcname) for source, arcname, _ in chunk)
            part_size += chunk_size

    if not parts[-1] and not first_part_extra and not skipped:
        return None
    return ArchivePlan(parts, first_part_extra, skipped, compression)


def _pack_archives(groups: list[FileGroup], first_part_extra: dict[str, bytes], compression: int) -> list[bytes]:
    plan = _plan_archives(groups, first_part_extra, compression)
    return [plan.build(i) for i in range(len(plan.parts))] if plan else []


def build_export_archives(
    filtered_rows: list[dict],
    all_rows: list[dict],
    campaign_title: str,
    filter_label: str,
    scope_label: str,
) -> list[bytes]:
    """Архив(ы) с логами, транскрипциями и статистикой, каждый не больше ARCHIVE_PART_LIMIT.
    Таблица (report.xlsx) и аудио отправляются отдельно, см. build_report_bytes и
    build_audio_archives.

    filtered_rows — чьи файлы попадут в архив (с учётом фильтра все/только ошибки).
    all_rows — полный набор за тот же период, по нему считается статистика (иначе при
    фильтре 'только ошибки' процент был бы всегда 100%)."""
    stats = build_stats_text(all_rows, campaign_title, filter_label, scope_label).encode("utf-8")
    return _pack_archives(_collect_files(filtered_rows, audio=False), {"stats.txt": stats}, zipfile.ZIP_DEFLATED)


_CALL_INFO_FIELDS = [
    ("Время", "timestamp"), ("Исполнитель", "name"), ("Телефон", "phone"), ("Кампания", "campaign"),
    ("Раунд", "round"), ("Тест-план", "test_plan"), ("Тест-кейс", "test_case"), ("Параметры", "params"),
    ("Курл", "status"), ("Ошибка курла", "error"), ("Результат", "call_result"), ("Комментарий", "comment"),
    ("sid", "sid"), ("call_id", "call_id"),
]
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\s]+')


def _call_folder(record: dict) -> str:
    when = str(record.get("timestamp") or "").replace("T", "_").replace(":", "-")
    name = str(record.get("call_id") or record.get("sid") or f"row{record['_row']}")
    return _UNSAFE_NAME_CHARS.sub("_", f"{when}_{record.get('name') or ''}_{name}").strip("_")


def _call_info(record: dict) -> str:
    return "\n".join(
        f"{label}: {record[key]}" for label, key in _CALL_INFO_FIELDS if record.get(key) not in (None, "", "-")
    )


def plan_calls_archives(records: list[dict]) -> ArchivePlan | None:
    """Всё по выбранным звонкам (строкам листа Calls), у каждого звонка своя папка:
    info.txt, логи, транскрипция и аудио. Если файлов уже нет на диске — кладём текст
    лога и транскрипции, сохранённый в самой таблице."""
    groups: list[FileGroup] = []
    for r in records:
        folder = _call_folder(r)
        group: FileGroup = [(_call_info(r).encode("utf-8"), f"{folder}/info.txt")]

        def add(path_str: str) -> bool:
            path = Path(path_str.strip())
            if path_str.strip() and path.exists():
                group.append((path, f"{folder}/{path.name}"))
                return True
            return False

        has_logs = any([add(p) for p in (r.get("log_file") or "").split("; ")])
        if not has_logs and r.get("log"):
            group.append((str(r["log"]).encode("utf-8"), f"{folder}/log_from_table.txt"))
        if not add(r.get("transcript_file") or "") and r.get("transcription"):
            group.append((str(r["transcription"]).encode("utf-8"), f"{folder}/transcript_from_table.txt"))
        add(r.get("audio_file") or "")
        groups.append(group)
    return _plan_archives(groups, {}, zipfile.ZIP_DEFLATED)


def build_audio_archives(filtered_rows: list[dict]) -> list[bytes]:
    """Аудиозаписи выбранных звонков, разбитые на архивы не больше ARCHIVE_PART_LIMIT.
    Пустой список, если записей нет. wav почти не сжимается, поэтому без компрессии."""
    return _pack_archives(_collect_files(filtered_rows, audio=True), {}, zipfile.ZIP_STORED)
