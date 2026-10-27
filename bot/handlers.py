import asyncio
import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import (
    access, audio_pause, campaigns, config, excel_store, export_service, latency, numbers_store, pause, pause_report,
    var_rotation, vlogs_fetcher,
)
from .api_client import trigger_call
from .audio_fetcher import download_recording
from .intent_annotator import annotate_transcript
from .config import LogSource
from .curl_parser import render_body
from .logs_fetcher import fetch_call_log
from .phone import validate_phone
from .transcript_fetcher import CallOutcome, wait_for_call_outcome

router = Router()
# Незарегистрированным — только /start и регистрация (см. access.py)
router.message.outer_middleware(access.AccessMiddleware())
router.callback_query.outer_middleware(access.AccessMiddleware())

class Registration(StatesGroup):
    password = State()
    name = State()
    phone = State()


class CommentState(StatesGroup):
    waiting = State()


class VarInput(StatesGroup):
    waiting = State()


def top_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📞 Сделать звонок", callback_data="menu:call")],
        [InlineKeyboardButton(text="📋 Тестирование по тест-кейсам", callback_data="menu:testcases")],
        [InlineKeyboardButton(text="📊 Экспорт отчёта", callback_data="menu:export")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="⚙️ Админка", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def test_case_nav_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Следующий тест-кейс", callback_data="tc_next")],
            [InlineKeyboardButton(text="🔁 Повторить тест-кейс", callback_data="tc_repeat")],
        ]
    )


def comment_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Успешный звонок", callback_data="comment:ok")],
            [InlineKeyboardButton(text="❌ Ошибка. Есть комментарий", callback_data="comment:error")],
        ]
    )


def campaign_pick_kb(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=c.title, callback_data=f"{prefix}:{c.key}")]
        for c in campaigns.all_campaigns()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def export_filter_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Все", callback_data="exp_filter:all")],
            [InlineKeyboardButton(text="Только ошибки", callback_data="exp_filter:errors")],
        ]
    )


def export_scope_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="За всё время", callback_data="exp_scope:all")],
            [InlineKeyboardButton(text="Только последняя кампания", callback_data="exp_scope:latest")],
        ]
    )


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await excel_store.get_user(message.from_user.id)
    if user is None:
        if _is_admin(message.from_user.id):
            await message.answer("Добро пожаловать! Как вас зовут?")
            await state.set_state(Registration.name)
            return
        await message.answer("Добро пожаловать! Введите пароль доступа к боту:")
        await state.set_state(Registration.password)
        return
    await message.answer(
        f"С возвращением, {user['name']}! Что делаем?",
        reply_markup=top_menu_kb(_is_admin(message.from_user.id)),
    )


@router.message(Registration.password)
async def registration_password(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    # Пароль не оставляем в истории чата
    try:
        await message.delete()
    except Exception:
        pass
    wait = access.lockout_left(user_id)
    if wait:
        await message.answer(f"⛔ Слишком много неверных попыток. Попробуйте через {wait // 60 + 1} мин.")
        return
    if not access.check_password(user_id, (message.text or "").strip()):
        wait = access.lockout_left(user_id)
        if wait:
            await message.answer(f"⛔ Неверный пароль. Попытки заблокированы на {wait // 60 + 1} мин.")
        else:
            await message.answer(f"❌ Неверный пароль. Осталось попыток: {access.attempts_left(user_id)}")
        return
    await message.answer("✅ Пароль верный. Как вас зовут?")
    await state.set_state(Registration.name)


@router.message(Registration.name)
async def registration_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Введите имя:")
        return
    await state.update_data(name=name)
    await message.answer("Введите номер телефона без плюса и пробелов (например 998901234567 или 79991234567):")
    await state.set_state(Registration.phone)


@router.message(Registration.phone)
async def registration_phone(message: Message, state: FSMContext) -> None:
    phone, error = validate_phone(message.text or "")
    if error:
        await message.answer(f"❌ {error}\nВведите номер ещё раз, например 998901234567:")
        return
    data = await state.get_data()
    await excel_store.register_user(message.from_user.id, data["name"], phone)
    access.mark_registered(message.from_user.id)
    await state.clear()

    try:
        db_result = await numbers_store.add_number_to_campaigns(phone)
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось добавить номер {phone} в БД: {exc}")
    else:
        if not db_result.added:
            await message.answer(f"ℹ️ Номер {phone} уже есть в БД.")
        elif not db_result.existing:
            await message.answer(f"✅ Номер {phone} добавлен в БД.")
        else:
            await message.answer(
                f"✅ Номер {phone} добавлен в БД для: {'; '.join(db_result.added)}\n"
                f"ℹ️ Уже был для: {'; '.join(db_result.existing)}"
            )

    await message.answer(
        "Регистрация завершена! Что делаем?",
        reply_markup=top_menu_kb(_is_admin(message.from_user.id)),
    )


@router.callback_query(F.data == "menu:call")
async def menu_call(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not campaigns.all_campaigns():
        await callback.message.answer("Пока нет ни одной кампании — попросите админа добавить курл.")
        return
    await callback.message.answer("Выберите сценарий:", reply_markup=campaign_pick_kb("campaign"))


@router.callback_query(F.data.startswith("campaign:"))
async def campaign_selected(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    campaign = campaigns.get(key)
    if campaign is None:
        await callback.answer("Кампания не найдена — возможно, админ её удалил", show_alert=True)
        return

    await state.update_data(campaign_key=key, extra_params={})
    await callback.answer()
    if not campaign.variables:
        await run_campaign_call(callback.message, state)
        return
    if campaign.var_list_problem() is not None:
        # Списка нет — остаётся только ввести значения вручную
        await _ask_next_variable(callback.message, state, campaign, {})
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="var:manual")],
            [InlineKeyboardButton(text="🎲 Из списка", callback_data="var:random")],
        ]
    )
    await callback.message.answer(f"Переменные: {', '.join(campaign.variables)}", reply_markup=kb)


@router.callback_query(F.data == "var:random")
async def var_random(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    # Пустые extra_params — _run_campaign_call сам возьмёт следующую строку из ротации
    await state.update_data(extra_params={})
    await run_campaign_call(callback.message, state)


@router.callback_query(F.data == "var:manual")
async def var_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    campaign = campaigns.get((await state.get_data()).get("campaign_key", ""))
    if campaign is None:
        await callback.message.answer("Кампания не найдена — выберите сценарий заново.")
        return
    await _ask_next_variable(callback.message, state, campaign, {})


async def _ask_next_variable(message: Message, state: FSMContext, campaign, values: dict) -> None:
    """Спрашивает переменные кампании по одной; когда введены все — запускает звонок."""
    remaining = [v for v in campaign.variables if v not in values]
    if not remaining:
        await state.set_state(None)
        await state.update_data(extra_params=values)
        await run_campaign_call(message, state)
        return
    await state.update_data(var_values=values)
    await state.set_state(VarInput.waiting)
    position = len(campaign.variables) - len(remaining) + 1
    await message.answer(f"Введите значение {remaining[0]} ({position} из {len(campaign.variables)}):")


@router.message(VarInput.waiting)
async def var_manual_input(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value:
        await message.answer("Значение не может быть пустым, введите ещё раз:")
        return
    data = await state.get_data()
    campaign = campaigns.get(data.get("campaign_key", ""))
    if campaign is None:
        await state.set_state(None)
        await message.answer("Кампания не найдена — возможно, админ её удалил. Выберите сценарий заново.")
        return
    values = data.get("var_values", {})
    remaining = [v for v in campaign.variables if v not in values]
    if remaining:
        values = {**values, remaining[0]: value}
    await _ask_next_variable(message, state, campaign, values)


# telegram_id пользователей, у которых сейчас идёт звонок: одновременно — только один
_active_calls: set[int] = set()


async def run_campaign_call(message: Message, state: FSMContext) -> None:
    user_id = message.chat.id
    if user_id in _active_calls:
        await message.answer("⏳ У вас уже идёт звонок — дождитесь, пока бот пришлёт по нему результат.")
        return
    _active_calls.add(user_id)
    try:
        await _run_campaign_call(message, state)
    finally:
        _active_calls.discard(user_id)


async def _run_campaign_call(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    campaign = campaigns.get(data.get("campaign_key", ""))
    if campaign is None:
        await message.answer("❌ Кампания не найдена — возможно, админ её удалил.")
        return
    values = data.get("extra_params", {})
    rotation_row = None
    if any(v not in values for v in campaign.variables):
        problem = campaign.var_list_problem()
        if problem:
            await message.answer(f"❌ Не удалось взять значения из списка: {problem}")
            return
        rotation_row, values = var_rotation.peek_next(message.chat.id, campaign)
    if campaign.variables:
        await message.answer("🧩 " + ", ".join(f"{v}: {values[v]}" for v in campaign.variables))

    user = await excel_store.get_user(message.chat.id)
    if user is None:
        await message.answer("Вас нет в списке пользователей — зарегистрируйтесь через /start.")
        return
    phone = user["phone"]
    name = user["name"]

    body = render_body(campaign.body, phone, values)
    # В Excel пишем то, чем звонки отличаются друг от друга: params (или всё тело без номера)
    params = body.get("params", {k: v for k, v in body.items() if k != "number"})

    round_number = await excel_store.ensure_round(campaign.key)

    call_started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
    result = await trigger_call(campaign.url, campaign.headers, body)

    test_case_desc = data.get("test_case_desc", "-")
    test_case_ctx = data.get("test_case_ctx")
    test_plan = test_case_ctx["test_plan"] if test_case_ctx and test_case_desc != "-" else "-"
    is_admin = _is_admin(message.chat.id)

    if not result.ok:
        await message.answer(f"❌ Курл не отправлен, ошибка:\n{result.error}")
        await _safe_excel(message, excel_store.add_call_row(
            timestamp=_now(), telegram_id=message.chat.id, name=name, phone=phone,
            campaign=campaign.title, params=json.dumps(params, ensure_ascii=False),
            status="error", error=result.error or "", round=round_number, test_case=test_case_desc,
            test_plan=test_plan,
        ))
        await state.clear()
        await _finish_call_interaction(message, state, test_case_ctx, is_admin)
        return

    if rotation_row is not None:
        var_rotation.commit(message.chat.id, campaign, rotation_row)

    await message.answer(
        f"✅ Звонок успешно запущен на номер {phone}\nКампания: {campaign.title}\nsid: {result.sid}"
    )

    row_index = await _safe_excel(message, excel_store.add_call_row(
        timestamp=_now(), telegram_id=message.chat.id, name=name, phone=phone,
        campaign=campaign.title, params=json.dumps(params, ensure_ascii=False),
        status="success", sid=result.sid or "", round=round_number, test_case=test_case_desc,
        test_plan=test_plan,
    ))

    try:
        await asyncio.wait_for(
            _process_call_result(message, state, campaign, phone, result, call_started_at, row_index),
            timeout=config.MAX_CALL_SESSION_SECONDS,
        )
    except asyncio.TimeoutError:
        minutes = int(config.MAX_CALL_SESSION_SECONDS // 60)
        await message.answer(
            f"⏱ Обработка звонка идёт дольше {minutes} минут — прерываю ожидание. "
            "Судя по всему, зависла не сама сессия звонка, а получение статуса/логов "
            "(БД или SSH). Проверьте результат вручную позже."
        )
        if row_index is not None:
            await _safe_excel(message, excel_store.update_call_row(row_index, call_result="timeout"))
        await state.clear()
        await _finish_call_interaction(message, state, test_case_ctx, is_admin)


async def _process_call_result(
    message: Message,
    state: FSMContext,
    campaign,
    phone: str,
    result,
    call_started_at: dt.datetime,
    row_index: int | None,
) -> None:
    await message.answer("⏳ Жду завершения звонка (проверяю БД)...")

    last_ping = 0.0

    async def _on_tick(elapsed: float) -> None:
        nonlocal last_ping
        if elapsed - last_ping >= config.PROGRESS_PING_SECONDS:
            last_ping = elapsed
            await message.answer(f"⏳ Всё ещё жду... ({int(elapsed)} сек.)")

    outcome, outcome_error = await _safe_wait_outcome(phone, campaign.campaign_id, call_started_at, _on_tick)

    since_seconds = int((dt.datetime.now(dt.timezone.utc) - call_started_at).total_seconds())
    since_seconds += config.LOG_FETCH_BUFFER_SECONDS
    log_texts = await asyncio.gather(
        *[_safe_fetch_log(source, result.sid, since_seconds) for source in config.LOG_SOURCES]
    )

    call_result_text = ""
    db_call_id = None
    rec_url = outcome.rec_file if outcome is not None else None
    if outcome_error is not None:
        transcript_text = f"(не удалось получить статус звонка из БД: {outcome_error})"
    elif not outcome.completed:
        transcript_text = "(звонок не завершился в БД за отведённое время ожидания)"
        db_call_id = outcome.call_id
    else:
        call_result_text = outcome.attempt_result or ""
        if outcome.transcribed:
            # К ответам пользователя дописываем интенты, определённые классификатором (из лога CPAAS)
            transcript_text = annotate_transcript(outcome.transcript or "", list(log_texts))
        else:
            transcript_text = "(транскрипция ещё не готова)"
        db_call_id = outcome.call_id

    # Файлы именуем call_id из БД (реальный идентификатор звонка), а не sid из campaigns API —
    # если БД ещё не отдала call_id, временно используем sid как запасной вариант.
    call_id = db_call_id or result.sid or f"nosid-{int(call_started_at.timestamp())}"
    call_dir = _call_log_dir(campaign.title, call_started_at)

    await message.answer(f"📞 Результат звонка (из БД): {call_result_text or 'неизвестно'}")

    # Все логи звонка: по SSH (ЛАБА, CPAAS studio) и из VictoriaLogs — по файлу на сервис
    log_files: list[tuple[str, str]] = [
        (f"{call_id}__{source.namespace}.txt", log_text)
        for source, log_text in zip(config.LOG_SOURCES, log_texts)
    ]
    vlog_lines, vlog_error = await _safe_fetch_vlogs([db_call_id, result.sid], call_started_at)
    for service, text in vlogs_fetcher.group_by_service(vlog_lines).items():
        log_files.append((f"{call_id}__{service}.txt", text))
    pause_turns = pause.extract_turn_pauses(vlog_lines)

    transcript_file_path = _save_text_file(call_dir, f"{call_id}_transcript.txt", transcript_text)
    await _send_as_file(message, "📝 Транскрипция", transcript_text, f"{call_id}_transcript.txt")

    audio_file_path = await _save_and_send_audio(message, call_dir, call_id, rec_url)

    # Пауза "как слышится" меряется по записи, поэтому отчёт и архив — после скачивания аудио
    heard_turns, heard_error = await _safe_measure_heard(audio_file_path, pause_turns, vlog_lines)
    if pause_turns:
        log_files.append((
            f"{call_id}_timing.txt",
            pause_report.format_timing_report(pause_turns, heard_turns, heard_error),
        ))
    log_file_paths = [_save_text_file(call_dir, name, text) for name, text in log_files]
    await _send_logs_archive(message, call_id, log_files)
    if vlog_error:
        await message.answer(f"⚠️ Логи из VictoriaLogs не получены: {vlog_error}")

    await message.answer(pause_report.format_call_pause(pause_turns, heard_turns, heard_error))
    pause_fields = {
        "pause_turns_ms": ";".join(str(t.pause_ms) for t in pause_turns),
        "heard_turns_ms": ";".join(str(v) for v in pause_report.heard_values(heard_turns)),
    }
    for prefix, stats in (
        ("pause", pause.pause_stats(pause_turns)),
        ("heard", latency.compute_stats(pause_report.heard_values(heard_turns))),
    ):
        if stats is not None:
            pause_fields.update({
                f"{prefix}_avg_ms": round(stats.avg_ms), f"{prefix}_median_ms": round(stats.median_ms),
                f"{prefix}_min_ms": stats.min_ms, f"{prefix}_max_ms": stats.max_ms,
            })

    combined_log = "\n\n".join(
        f"[{source.label}]\n{text}" for source, text in zip(config.LOG_SOURCES, log_texts)
    )
    if row_index is not None:
        await _safe_excel(message, excel_store.update_call_row(
            row_index, call_id=call_id, call_result=call_result_text, log=combined_log,
            log_file="; ".join(log_file_paths),
            transcription=transcript_text, transcript_file=transcript_file_path,
            audio_file=audio_file_path, **pause_fields,
        ))

    await state.update_data(pending_row=row_index)
    await message.answer("Как прошёл звонок?", reply_markup=comment_choice_kb())


async def _safe_fetch_vlogs(
    call_ids: list[str | None], call_started_at: dt.datetime
) -> tuple[list[vlogs_fetcher.VLogLine], str | None]:
    if not config.VLOGS_URL:
        return [], None
    padding = dt.timedelta(seconds=config.VLOGS_WINDOW_PADDING_SECONDS)
    start = call_started_at - padding
    end = dt.datetime.now(dt.timezone.utc) + padding
    try:
        return await vlogs_fetcher.fetch_call_logs([i for i in call_ids if i], start, end), None
    except Exception as exc:
        return [], str(exc) or type(exc).__name__


async def _safe_measure_heard(
    audio_file_path: str, turns: list[pause.TurnPause], lines: list[vlogs_fetcher.VLogLine]
) -> tuple[list[audio_pause.HeardTurn], str]:
    if not audio_file_path:
        return [], "аудиозапись не получена"
    try:
        return await asyncio.to_thread(audio_pause.measure_heard_pauses, Path(audio_file_path), turns, lines)
    except Exception as exc:
        return [], f"не удалось разобрать запись: {exc}"


async def _send_logs_archive(message: Message, call_id: str, log_files: list[tuple[str, str]]) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in log_files:
            zf.writestr(name, text)
    await message.answer_document(
        BufferedInputFile(buf.getvalue(), filename=f"{call_id}_logs.zip"),
        caption=f"📦 Логи звонка: {len(log_files)} файлов",
    )


async def _safe_fetch_log(source: LogSource, sid: str | None, since_seconds: int) -> str:
    if not sid:
        return "(sid отсутствует)"
    try:
        return await fetch_call_log(source, sid, since_seconds)
    except Exception as exc:
        return f"(не удалось получить лог: {exc})"


_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*]')


def _sanitize_folder_name(name: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", name).strip()


def _call_log_dir(campaign_title: str, when: dt.datetime) -> Path:
    day_folder = when.strftime("%Y-%m-%d")
    path = Path(config.LOGS_DIR) / _sanitize_folder_name(campaign_title) / day_folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_text_file(directory: Path, filename: str, text: str) -> str:
    path = directory / filename
    path.write_text(text, encoding="utf-8")
    return str(path)


async def _send_as_file(message: Message, caption: str, text: str, filename: str) -> None:
    await message.answer_document(
        BufferedInputFile(text.encode("utf-8"), filename=filename), caption=caption
    )


# Лимит Telegram Bot API на отправку файла ботом
_TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024


async def _save_and_send_audio(message: Message, call_dir: Path, call_id: str, rec_url: str | None) -> str:
    """Скачивает аудиозапись звонка в папку с логами и отправляет её в чат.
    Возвращает путь к сохранённому файлу (или пустую строку, если записи нет)."""
    if not rec_url:
        await message.answer("🎧 Аудиозапись звонка в БД не найдена")
        return ""
    path = call_dir / f"{call_id}.wav"
    try:
        size = await download_recording(rec_url, path)
    except Exception as exc:
        await message.answer(f"🎧 Не удалось скачать аудиозапись: {exc}\n{rec_url}")
        return ""
    if size > _TELEGRAM_UPLOAD_LIMIT:
        await message.answer(f"🎧 Аудиозапись слишком большая для Telegram, ссылка:\n{rec_url}")
    else:
        await message.answer_audio(FSInputFile(path, filename=path.name), caption="🎧 Аудиозапись звонка")
    return str(path)


async def _safe_wait_outcome(
    phone: str, campaign_id: str, since: dt.datetime, on_tick
) -> tuple[CallOutcome | None, str | None]:
    try:
        return await wait_for_call_outcome(phone, campaign_id, since, on_tick=on_tick), None
    except Exception as exc:
        return None, str(exc)


async def _safe_excel(message: Message, coro):
    """Не даёт сбою записи в Excel (например, файл открыт в самой Excel) уронить весь диалог."""
    try:
        return await coro
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось записать в Excel: {exc}")
        return None


async def _finish_call_interaction(
    message: Message, state: FSMContext, test_case_ctx: dict | None, is_admin: bool
) -> None:
    """После звонка (успех/ошибка/таймаут/комментарий) — если это был тест-кейс,
    предлагаем перейти к следующему/повторить; иначе возвращаем в главное меню."""
    if test_case_ctx:
        await state.update_data(test_case_ctx=test_case_ctx)
        await message.answer("Что дальше?", reply_markup=test_case_nav_kb())
    else:
        await message.answer("Выберите действие:", reply_markup=top_menu_kb(is_admin))


@router.callback_query(F.data == "comment:ok")
async def comment_ok(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    row_index = data.get("pending_row")
    test_case_ctx = data.get("test_case_ctx")
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    is_admin = _is_admin(callback.from_user.id)
    if row_index is None:
        await callback.message.answer("⚠️ Запись об этом звонке не сохранилась в Excel ранее, отметить некуда.")
        await _finish_call_interaction(callback.message, state, test_case_ctx, is_admin)
        return
    await _safe_excel(callback.message, excel_store.update_call_row(row_index, comment="Успешный звонок"))
    await callback.message.answer("Записано. Спасибо!")
    await _finish_call_interaction(callback.message, state, test_case_ctx, is_admin)


@router.callback_query(F.data == "comment:error")
async def comment_error(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Опишите, что пошло не так:")
    await state.set_state(CommentState.waiting)


@router.message(CommentState.waiting)
async def save_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    row_index = data.get("pending_row")
    test_case_ctx = data.get("test_case_ctx")
    comment = (message.text or "").strip()
    await state.clear()
    is_admin = _is_admin(message.from_user.id)
    if row_index is None:
        await message.answer("⚠️ Запись об этом звонке не сохранилась в Excel ранее, комментарий добавить некуда.")
        await _finish_call_interaction(message, state, test_case_ctx, is_admin)
        return
    await _safe_excel(message, excel_store.update_call_row(row_index, comment=comment))
    await message.answer("Комментарий сохранён. Спасибо!")
    await _finish_call_interaction(message, state, test_case_ctx, is_admin)


def _is_admin(telegram_id: int) -> bool:
    return access.is_admin(telegram_id)


# --- Экспорт ---

async def _start_export_flow(user_id: int, message: Message, state: FSMContext) -> None:
    user = await excel_store.get_user(user_id)
    if user is None:
        await message.answer("Сначала зарегистрируйтесь через /start.")
        return
    await state.update_data(export={})
    await message.answer("Выберите сценарий для выгрузки:", reply_markup=campaign_pick_kb("expcamp"))


@router.message(Command("export"))
async def cmd_export(message: Message, state: FSMContext) -> None:
    await _start_export_flow(message.from_user.id, message, state)


@router.callback_query(F.data == "menu:export")
async def menu_export(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_export_flow(callback.from_user.id, callback.message, state)


@router.callback_query(F.data.startswith("expcamp:"))
async def export_campaign_selected(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    campaign = campaigns.get(key)
    if campaign is None:
        await callback.answer("Кампания не найдена — возможно, админ её удалил", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    export_data = data.get("export", {})
    export_data["campaign_key"] = key
    await state.update_data(export=export_data)
    await callback.message.answer(
        f"Кампания: {campaign.title}\nКакие звонки включить?", reply_markup=export_filter_kb()
    )


@router.callback_query(F.data.startswith("exp_filter:"))
async def export_filter_selected(callback: CallbackQuery, state: FSMContext) -> None:
    only_errors = callback.data.split(":", 1)[1] == "errors"
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    export_data = data.get("export", {})
    export_data["only_errors"] = only_errors
    await state.update_data(export=export_data)
    await callback.message.answer("За какой период?", reply_markup=export_scope_kb())


@router.callback_query(F.data.startswith("exp_scope:"))
async def export_scope_selected(callback: CallbackQuery, state: FSMContext) -> None:
    only_latest_round = callback.data.split(":", 1)[1] == "latest"
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    export_data = data.get("export", {})
    campaign = campaigns.get(export_data.get("campaign_key", ""))
    if campaign is None:
        await callback.message.answer("Кампания не найдена — начните выгрузку заново.")
        return
    only_errors = export_data.get("only_errors", False)

    is_admin = _is_admin(callback.from_user.id)
    telegram_id_filter = None if is_admin else callback.from_user.id

    round_filter = None
    if only_latest_round:
        round_filter = await excel_store.get_current_round(campaign.key)

    await callback.message.answer("⏳ Собираю выгрузку...")
    filtered_rows = await excel_store.query_calls(campaign.title, telegram_id_filter, only_errors, round_filter)

    if not filtered_rows:
        await callback.message.answer("Не нашлось ни одного звонка под эти условия.")
        return

    # Статистику всегда считаем по полному набору звонков за период, а не по
    # отфильтрованным "только ошибки" — иначе процент ошибок был бы всегда 100%.
    all_rows = filtered_rows if not only_errors else await excel_store.query_calls(
        campaign.title, telegram_id_filter, False, round_filter
    )

    filter_label = "только ошибки" if only_errors else "все"
    scope_label = f"последняя кампания (раунд {round_filter})" if only_latest_round else "за всё время"

    scope_token = f"раунд{round_filter}" if round_filter is not None else "завсевремя"
    date_token = dt.datetime.now().strftime("%d_%m_%Y")
    base_name = f"{_sanitize_folder_name(campaign.title)}_{scope_token}_{date_token}"

    report_bytes = export_service.build_report_bytes(filtered_rows)
    archives = export_service.build_export_archives(
        filtered_rows, all_rows, campaign.title, filter_label, scope_label
    )
    stats_text = export_service.build_stats_text(all_rows, campaign.title, filter_label, scope_label)

    await callback.message.answer(stats_text)
    await callback.message.answer_document(
        BufferedInputFile(report_bytes, filename=f"report_{base_name}.xlsx"),
    )
    await _send_archive_parts(callback.message, archives, f"export_{base_name}")
    audio_archives = export_service.build_audio_archives(filtered_rows)
    if audio_archives:
        await _send_archive_parts(callback.message, audio_archives, f"audio_{base_name}", caption="🎧 Аудиозаписи")
    else:
        await callback.message.answer("🎧 Аудиозаписей по этим звонкам нет")


async def _send_archive_parts(message: Message, archives: list[bytes], name: str, caption: str | None = None) -> None:
    for i, archive_bytes in enumerate(archives, start=1):
        suffix = f"_part{i}of{len(archives)}" if len(archives) > 1 else ""
        part_caption = f"{caption} ({i}/{len(archives)})" if caption and len(archives) > 1 else caption
        await message.answer_document(
            BufferedInputFile(archive_bytes, filename=f"{name}{suffix}.zip"), caption=part_caption,
        )


# --- Тестирование по тест-кейсам ---

@router.callback_query(F.data == "menu:testcases")
async def menu_testcases(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    plans = await excel_store.list_test_plans()
    if not plans:
        await callback.message.answer("Пока нет ни одного загруженного тест-плана.")
        return
    await state.update_data(test_plans=[name for name, _ in plans])
    buttons = [
        [InlineKeyboardButton(text=f"{name} ({count} кейсов)", callback_data=f"tcplan:{i}")]
        for i, (name, count) in enumerate(plans)
    ]
    await callback.message.answer("Выберите тест-план:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("tcplan:"))
async def testplan_selected(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    plans = data.get("test_plans", [])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if idx >= len(plans):
        await callback.message.answer("Список устарел, откройте «Тестирование по тест-кейсам» заново.")
        return
    test_plan = plans[idx]
    current_index = await excel_store.get_progress(callback.from_user.id, test_plan)
    await _show_test_case(callback.message, state, test_plan, current_index, callback.from_user.id)


async def _show_test_case(
    message: Message, state: FSMContext, test_plan: str, index: int, telegram_id: int
) -> None:
    cases = await excel_store.get_test_plan_cases(test_plan)
    if not cases:
        await message.answer(f"В тест-плане «{test_plan}» не осталось тест-кейсов.")
        return
    if index >= len(cases):
        await message.answer(
            f"🎉 Все тест-кейсы плана «{test_plan}» пройдены!",
            reply_markup=top_menu_kb(_is_admin(telegram_id)),
        )
        return

    case = cases[index]
    campaign = campaigns.by_title(case["campaign"])
    if campaign is None:
        await message.answer(
            f"⚠️ Тест-кейс {index + 1}: сценарий «{case['campaign']}» не найден в текущем меню. "
            "Пропускаю, сообщите админу.",
        )
        await excel_store.set_progress(telegram_id, test_plan, index + 1)
        await _show_test_case(message, state, test_plan, index + 1, telegram_id)
        return

    await state.update_data(
        campaign_key=campaign.key, extra_params={}, test_case_desc=case["description"],
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="☎️ Позвонить", callback_data="tc_call")]]
    )
    await message.answer(
        f"Тест-кейс {index + 1} из {len(cases)}\n{case['description']}", reply_markup=kb
    )
    await state.update_data(test_plan=test_plan, tc_index=index)


@router.callback_query(F.data == "tc_call")
async def tc_call(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await state.update_data(test_case_ctx={"test_plan": data["test_plan"], "index": data["tc_index"]})
    campaign = campaigns.get(data.get("campaign_key", ""))
    if campaign is not None and campaign.variables and campaign.var_list_problem() is not None:
        # Списка переменных нет — значения спрашиваем вручную, иначе берутся из списка по кругу
        await _ask_next_variable(callback.message, state, campaign, {})
        return
    await run_campaign_call(callback.message, state)


@router.callback_query(F.data == "tc_next")
async def tc_next(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    ctx = data.get("test_case_ctx")
    if not ctx:
        await callback.message.answer(
            "Контекст тест-кейса потерян, откройте меню заново.",
            reply_markup=top_menu_kb(_is_admin(callback.from_user.id)),
        )
        return
    new_index = ctx["index"] + 1
    await excel_store.set_progress(callback.from_user.id, ctx["test_plan"], new_index)
    await _show_test_case(callback.message, state, ctx["test_plan"], new_index, callback.from_user.id)


@router.callback_query(F.data == "tc_repeat")
async def tc_repeat(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    ctx = data.get("test_case_ctx")
    if not ctx:
        await callback.message.answer(
            "Контекст тест-кейса потерян, откройте меню заново.",
            reply_markup=top_menu_kb(_is_admin(callback.from_user.id)),
        )
        return
    await _show_test_case(callback.message, state, ctx["test_plan"], ctx["index"], callback.from_user.id)
