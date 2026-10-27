import asyncio
import dataclasses
import datetime as dt
import html
import io

from aiogram import F, Router
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import access, admin_files, campaigns, excel_store, export_service, numbers_store, testcase_service
from .curl_parser import CurlError, ParsedCurl, find_variables, parse_curl, render_curl
from .handlers import _safe_excel, _sanitize_folder_name, _send_archive_parts, campaign_pick_kb, top_menu_kb
from .phone import validate_phone

# Админка: кампании (курлы) и списки переменных, тест-планы, пользователи, логи звонков.
# Подключается внутрь основного router (см. main.py), поэтому проходит через AccessMiddleware.
router = Router()


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return access.is_admin(event.from_user.id)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminInput(StatesGroup):
    curl_new = State()
    campaign_title = State()
    curl_replace = State()
    var_list_file = State()
    testcases_file = State()
    users_file = State()
    user_name = State()
    user_phone = State()
    log_search = State()


# Telegram не даёт больше 100 кнопок в одной клавиатуре
_MAX_LIST_BUTTONS = 90

CURL_EXAMPLE = (
    "curl -X POST 'http://host:8080/campaigns/<id кампании>/calls' "
    "-H 'Content-Type: application/json' "
    "-d '{\"number\": \"998900000000\", \"params\": {\"client_lang\": \"ru\", \"brand\": \"{{brand}}\"}}'"
)


def _kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows]
    )


CANCEL_KB = _kb(("↩️ Отмена", "adm:cancel"))


def admin_menu_kb() -> InlineKeyboardMarkup:
    return _kb(
        ("🧩 Кампании (курлы)", "adm:camps"),
        ("🗂 Тест-планы", "menu:testplans"),
        ("👥 Пользователи", "adm:users"),
        ("📜 Логи звонков", "adm:logs"),
        ("🆕 Новый раунд тестирования", "menu:new_campaign"),
        ("↩️ Главное меню", "adm:home"),
    )


async def _show_admin_menu(message: Message) -> None:
    await message.answer("⚙️ Админка", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "menu:admin")
async def menu_admin(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_admin_menu(callback.message)


@router.callback_query(F.data == "adm:home")
async def admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Выберите действие:", reply_markup=top_menu_kb(True))


@router.callback_query(F.data == "adm:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await _show_admin_menu(callback.message)


async def _download(message: Message) -> io.BytesIO:
    buf = io.BytesIO()
    await message.bot.download(message.document, destination=buf)
    buf.seek(0)
    return buf


def _date_token() -> str:
    return dt.datetime.now().strftime("%d_%m_%Y")


# --- Новый раунд тестирования ---

@router.message(Command("new_campaign"))
async def cmd_new_campaign(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Для какого сценария начать новый раунд тестирования?", reply_markup=campaign_pick_kb("newcamp")
    )


@router.callback_query(F.data == "menu:new_campaign")
async def menu_new_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await cmd_new_campaign(callback.message, state)


@router.callback_query(F.data.startswith("newcamp:"))
async def new_campaign_selected(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = campaigns.get(callback.data.split(":", 1)[1])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if campaign is None:
        await callback.message.answer("Кампания не найдена.")
        return
    new_round = await excel_store.create_new_round(campaign.key, callback.from_user.id)
    await callback.message.answer(
        f"Кампания «{campaign.title}»: создан раунд №{new_round}.\n"
        "Все новые звонки по этому сценарию теперь относятся к этому раунду."
    )


# --- Кампании (курлы) и списки переменных ---

def _campaign_card(c: campaigns.Campaign) -> str:
    lines = [f"<b>{html.escape(c.title)}</b>", f"campaign_id: <code>{c.campaign_id}</code>"]
    if c.variables:
        lines.append(f"Переменные: {html.escape(', '.join(c.variables))}")
        if c.var_list is not None:
            lines.append(
                f"Список: {len(c.var_list.rows)} строк, колонки: {html.escape(', '.join(c.var_list.columns))}"
            )
        problem = c.var_list_problem()
        if problem:
            lines.append(f"⚠️ {html.escape(problem)} — исполнители будут вводить значения вручную")
    else:
        lines.append("Переменных нет")
    lines += ["", f"<pre>{html.escape(render_curl(c.url, c.headers, c.body))}</pre>"]
    return "\n".join(lines)


def _campaign_card_kb(c: campaigns.Campaign) -> InlineKeyboardMarkup:
    rows = [("🔄 Заменить курл", f"adm:camp_repl:{c.key}")]
    if c.variables:
        rows.append(("📥 Загрузить список переменных", f"adm:vl_up:{c.key}"))
    if c.var_list is not None:
        rows += [
            ("📤 Выгрузить список переменных", f"adm:vl_down:{c.key}"),
            ("🗑 Удалить список переменных", f"adm:vl_del:{c.key}"),
        ]
    rows += [("🗑 Удалить кампанию", f"adm:camp_del:{c.key}"), ("↩️ К списку кампаний", "adm:camps")]
    return _kb(*rows)


async def _show_campaign(message: Message, c: campaigns.Campaign) -> None:
    await message.answer(_campaign_card(c), parse_mode="HTML", reply_markup=_campaign_card_kb(c))


async def _campaign_from_callback(callback: CallbackQuery) -> campaigns.Campaign | None:
    """Кампания из callback_data вида adm:<действие>:<key>."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    campaign = campaigns.get(callback.data.split(":", 2)[2])
    if campaign is None:
        await callback.message.answer("Кампания не найдена — возможно, её уже удалили.")
    return campaign


async def _add_users_to_campaign(message: Message, campaign_id: str) -> None:
    """Номера всех исполнителей заводятся в новую кампанию так же, как при регистрации."""
    users = await excel_store.list_users()
    try:
        added = await numbers_store.add_numbers_to_campaign([u["phone"] for u in users], campaign_id)
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось добавить номера исполнителей в кампанию в БД: {exc}")
        return
    await message.answer(f"ℹ️ Номеров исполнителей добавлено в кампанию в БД: {added}")


@router.callback_query(F.data == "adm:camps")
async def admin_campaigns(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    items = campaigns.all_campaigns()
    if items:
        listing = "\n".join(
            f"• {c.title}" + (f" — переменные: {', '.join(c.variables)}" if c.variables else "")
            for c in items
        )
        text = f"Кампании:\n{listing}\n\nВыберите кампанию, чтобы посмотреть курл и списки:"
    else:
        text = "Кампаний пока нет."
    rows = [(c.title, f"adm:camp:{c.key}") for c in items]
    rows += [("➕ Добавить курл", "adm:camp_add"), ("↩️ Админка", "menu:admin")]
    await callback.message.answer(text, reply_markup=_kb(*rows))


@router.callback_query(F.data.startswith("adm:camp:"))
async def admin_campaign_card(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is not None:
        await _show_campaign(callback.message, campaign)


@router.callback_query(F.data == "adm:camp_add")
async def admin_campaign_add(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(AdminInput.curl_new)
    await callback.message.answer(
        "Пришлите curl целиком одним сообщением.\n\n"
        "• В поле number бот сам подставит номер исполнителя.\n"
        "• Переменные отметьте как {{имя}} — их значения возьмутся из списка кампании или "
        "будут введены вручную. Всё остальное отправляется как есть.\n\n"
        f"Пример:\n{CURL_EXAMPLE}",
        reply_markup=CANCEL_KB,
    )


def _parse_curl_message(message: Message) -> tuple[ParsedCurl | None, str | None]:
    try:
        return parse_curl(message.text or ""), None
    except CurlError as exc:
        return None, f"❌ Курл не разобран: {exc}\nИсправьте и пришлите ещё раз."


@router.message(AdminInput.curl_new)
async def admin_campaign_curl(message: Message, state: FSMContext) -> None:
    parsed, error = _parse_curl_message(message)
    if error:
        await message.answer(error, reply_markup=CANCEL_KB)
        return
    await state.update_data(new_curl=dataclasses.asdict(parsed))
    await state.set_state(AdminInput.campaign_title)
    variables = find_variables(parsed.body)
    await message.answer(
        f"✅ Курл разобран.\ncampaign_id: {parsed.campaign_id}\n"
        f"Переменные: {', '.join(variables) if variables else 'нет'}\n\n"
        "Как назвать кампанию в меню бота? Это же название пишется в колонке campaign тест-планов.",
        reply_markup=CANCEL_KB,
    )


@router.message(AdminInput.campaign_title)
async def admin_campaign_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    if not title:
        await message.answer("Название не может быть пустым:", reply_markup=CANCEL_KB)
        return
    if campaigns.by_title(title) is not None:
        await message.answer(f"Кампания «{title}» уже есть — введите другое название:", reply_markup=CANCEL_KB)
        return
    data = await state.get_data()
    await state.clear()
    campaign = campaigns.add(title, ParsedCurl(**data["new_curl"]))
    await message.answer(f"✅ Кампания «{title}» добавлена.")
    await _add_users_to_campaign(message, campaign.campaign_id)
    await _show_campaign(message, campaign)


@router.callback_query(F.data.startswith("adm:camp_repl:"))
async def admin_campaign_replace(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is None:
        return
    await state.set_state(AdminInput.curl_replace)
    await state.update_data(edit_campaign=campaign.key)
    await callback.message.answer(
        f"Пришлите новый curl для «{campaign.title}». Название, история звонков и раунды сохранятся.",
        reply_markup=CANCEL_KB,
    )


@router.message(AdminInput.curl_replace)
async def admin_campaign_replace_curl(message: Message, state: FSMContext) -> None:
    parsed, error = _parse_curl_message(message)
    if error:
        await message.answer(error, reply_markup=CANCEL_KB)
        return
    key = (await state.get_data()).get("edit_campaign", "")
    await state.clear()
    old = campaigns.get(key)
    if old is None:
        await message.answer("Кампания не найдена — возможно, её уже удалили.")
        return
    campaign = campaigns.replace_curl(key, parsed)
    await message.answer(f"✅ Курл кампании «{campaign.title}» заменён.")
    if campaign.campaign_id != old.campaign_id:
        await _add_users_to_campaign(message, campaign.campaign_id)
    await _show_campaign(message, campaign)


@router.callback_query(F.data.startswith("adm:camp_del:"))
async def admin_campaign_delete(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is None:
        return
    await callback.message.answer(
        f"Удалить кампанию «{campaign.title}» вместе со списком переменных? "
        "История звонков в Excel и папка с логами останутся.",
        reply_markup=_kb(("✅ Да, удалить", f"adm:camp_delyes:{campaign.key}"), ("↩️ Отмена", "adm:camps")),
    )


@router.callback_query(F.data.startswith("adm:camp_delyes:"))
async def admin_campaign_delete_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is None:
        return
    campaigns.delete(campaign.key)
    await callback.message.answer(
        f"Кампания «{campaign.title}» удалена.",
        reply_markup=_kb(("↩️ К списку кампаний", "adm:camps")),
    )


@router.callback_query(F.data.startswith("adm:vl_up:"))
async def admin_var_list_upload(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is None:
        return
    await state.set_state(AdminInput.var_list_file)
    await state.update_data(edit_campaign=campaign.key)
    await callback.message.answer(
        f"Пришлите список переменных для «{campaign.title}» файлом .xlsx или .csv.\n\n"
        f"Первая строка — имена переменных: {', '.join(campaign.variables)} (порядок любой, "
        "лишние колонки можно оставить). Каждая следующая строка — один набор значений на звонок.\n"
        "Текущий список, если он есть, будет заменён.",
        reply_markup=CANCEL_KB,
    )


@router.message(AdminInput.var_list_file, F.document)
async def admin_var_list_file(message: Message, state: FSMContext) -> None:
    campaign = campaigns.get((await state.get_data()).get("edit_campaign", ""))
    if campaign is None:
        await state.clear()
        await message.answer("Кампания не найдена — возможно, её уже удалили.")
        return
    filename = message.document.file_name or ""
    if not filename.lower().endswith((".xlsx", ".xlsm", ".csv")):
        await message.answer("Нужен файл .xlsx или .csv.", reply_markup=CANCEL_KB)
        return
    var_list, errors = admin_files.parse_var_list_file(await _download(message), filename, campaign)
    if errors:
        await message.answer(
            "В файле есть проблемы:\n" + "\n".join(f"• {e}" for e in errors) + "\n\nИсправьте и пришлите ещё раз.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.clear()
    campaigns.set_var_list(campaign.key, var_list)
    await message.answer(f"✅ Список загружен: {len(var_list.rows)} строк. Ротация у исполнителей начнётся заново.")
    await _show_campaign(message, campaign)


@router.callback_query(F.data.startswith("adm:vl_down:"))
async def admin_var_list_download(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is None:
        return
    if campaign.var_list is None:
        await callback.message.answer("У кампании нет списка переменных.")
        return
    await callback.message.answer_document(BufferedInputFile(
        admin_files.build_var_list_file(campaign.var_list),
        filename=f"variables_{_sanitize_folder_name(campaign.title)}.xlsx",
    ))
    await _show_campaign(callback.message, campaign)


@router.callback_query(F.data.startswith("adm:vl_del:"))
async def admin_var_list_delete(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is None:
        return
    campaigns.set_var_list(campaign.key, None)
    await callback.message.answer("Список переменных удалён.")
    await _show_campaign(callback.message, campaign)


# --- Тест-планы (загрузка / выгрузка / удаление) ---

def testplans_admin_kb() -> InlineKeyboardMarkup:
    return _kb(
        ("📥 Загрузить тест-план(ы)", "menu:upload_testcases"),
        ("📤 Выгрузить тест-планы", "tp_download"),
        ("🗑 Удалить тест-план", "tpdel_list"),
        ("↩️ Админка", "menu:admin"),
    )


@router.callback_query(F.data == "menu:testplans")
async def menu_testplans(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    plans = await excel_store.list_test_plans()
    if plans:
        listing = "\n".join(f"• {name} — {count} кейсов" for name, count in plans)
        text = f"Загруженные тест-планы:\n{listing}"
    else:
        text = "Пока нет ни одного загруженного тест-плана."
    await callback.message.answer(text, reply_markup=testplans_admin_kb())


@router.callback_query(F.data == "tp_download")
async def testplans_download(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    rows = await excel_store.all_test_cases()
    if not rows:
        await callback.message.answer("Выгружать нечего — тест-планов нет.")
        return
    await callback.message.answer_document(
        BufferedInputFile(admin_files.build_testcases_file(rows), filename=f"testplans_{_date_token()}.xlsx"),
        caption="Этот же файл после правок можно загрузить обратно",
    )


@router.callback_query(F.data == "tpdel_list")
async def testplan_delete_list(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    plans = await excel_store.list_test_plans()
    if not plans:
        await callback.message.answer("Удалять нечего — тест-планов нет.")
        return
    # В callback_data имя плана не влезет (лимит 64 байта), поэтому держим список в state и передаём индекс.
    await state.update_data(del_test_plans=[name for name, _ in plans])
    buttons = [
        [InlineKeyboardButton(text=f"🗑 {name} ({count} кейсов)", callback_data=f"tpdel:{i}")]
        for i, (name, count) in enumerate(plans)
    ]
    buttons.append([InlineKeyboardButton(text="↩️ Отмена", callback_data="tpdel_cancel")])
    await callback.message.answer(
        "Какой тест-план удалить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


def _pending_delete_plan(data: dict, idx: int) -> str | None:
    plans = data.get("del_test_plans", [])
    return plans[idx] if 0 <= idx < len(plans) else None


@router.callback_query(F.data.startswith("tpdel:"))
async def testplan_delete_selected(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    idx = int(callback.data.split(":", 1)[1])
    test_plan = _pending_delete_plan(await state.get_data(), idx)
    if test_plan is None:
        await callback.message.answer("Список устарел, откройте «Тест-планы» заново.")
        return
    await callback.message.answer(
        f"Удалить тест-план «{test_plan}» со всеми тест-кейсами и прогрессом исполнителей?",
        reply_markup=_kb(("✅ Да, удалить", f"tpdel_yes:{idx}"), ("↩️ Отмена", "tpdel_cancel")),
    )


@router.callback_query(F.data.startswith("tpdel_yes:"))
async def testplan_delete_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    idx = int(callback.data.split(":", 1)[1])
    test_plan = _pending_delete_plan(await state.get_data(), idx)
    if test_plan is None:
        await callback.message.answer("Список устарел, откройте «Тест-планы» заново.")
        return
    removed = await _safe_excel(callback.message, excel_store.delete_test_plan(test_plan))
    await state.update_data(del_test_plans=None)
    if removed is not None:
        await callback.message.answer(f"Тест-план «{test_plan}» удалён ({removed} тест-кейсов).")
    await _show_admin_menu(callback.message)


@router.callback_query(F.data == "tpdel_cancel")
async def testplan_delete_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(del_test_plans=None)
    await _show_admin_menu(callback.message)


@router.callback_query(F.data == "menu:upload_testcases")
async def menu_upload_testcases(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.answer(
        "Отправьте Excel-файл (.xlsx) с тест-кейсами.\n\n"
        "В одном файле может быть несколько тест-планов — разные значения в колонке test_plan. "
        "План с уже существующим именем будет заменён, остальные планы не затрагиваются.\n\n"
        "Столбцы: test_plan, order, description, campaign.\n"
        "campaign должен точно совпадать с названием сценария в меню, например: "
        + ", ".join(f"«{c.title}»" for c in campaigns.all_campaigns()),
        reply_markup=CANCEL_KB,
    )
    await state.set_state(AdminInput.testcases_file)


@router.message(AdminInput.testcases_file, F.document)
async def receive_testcases_file(message: Message, state: FSMContext) -> None:
    await state.clear()
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith((".xlsx", ".xlsm")):
        await message.answer("Нужен файл .xlsx. Откройте «Тест-планы» → «Загрузить» ещё раз и пришлите файл.")
        return

    rows, errors = testcase_service.parse_testcases_file(await _download(message))
    if errors:
        await message.answer("В файле есть проблемы:\n" + "\n".join(f"• {e}" for e in errors))
        return

    summary = await excel_store.replace_test_plan_cases(rows)
    lines = [f"«{plan}»: {count} тест-кейс(ов)" for plan, count in summary.items()]
    await message.answer("Загружено:\n" + "\n".join(lines), reply_markup=admin_menu_kb())


# --- Пользователи ---

def _user_line(u: dict) -> str:
    role = " 👑 админ (.env)" if access.is_env_admin(u["telegram_id"]) else (" 👑 админ" if u["is_admin"] else "")
    return f"{u['name']} — {u['phone']} — id {u['telegram_id']}{role}"


async def _show_users(message: Message) -> None:
    users = await excel_store.list_users()
    text = "Пользователи:\n" + "\n".join(f"• {_user_line(u)}" for u in users) if users else "Пользователей пока нет."
    if len(text) > 4000:
        text = text[:4000] + "\n…(полный список — в выгрузке Excel)"
    rows = [(f"{u['name']} · {u['phone']}", f"adm:user:{u['telegram_id']}") for u in users[:_MAX_LIST_BUTTONS]]
    rows += [
        ("📥 Загрузить исправленный Excel", "adm:users_up"),
        ("↩️ Админка", "menu:admin"),
    ]
    await message.answer(text, reply_markup=_kb(*rows))


async def _show_user(message: Message, telegram_id: int) -> None:
    user = next((u for u in await excel_store.list_users() if u["telegram_id"] == telegram_id), None)
    if user is None:
        await message.answer("Пользователь не найден.", reply_markup=_kb(("↩️ К списку", "adm:users_list")))
        return
    rows = [("✏️ Изменить имя", f"adm:uname:{telegram_id}"), ("📞 Изменить номер", f"adm:uphone:{telegram_id}")]
    if not access.is_env_admin(telegram_id):
        rows.append(("👑 Снять админа" if user["is_admin"] else "👑 Сделать админом", f"adm:uadm:{telegram_id}"))
    rows += [("🗑 Удалить", f"adm:udel:{telegram_id}"), ("↩️ К списку", "adm:users_list")]
    await message.answer(
        f"{_user_line(user)}\nЗарегистрирован: {user['registered_at'] or '—'}", reply_markup=_kb(*rows)
    )


async def _user_id_from_callback(callback: CallbackQuery) -> int:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    return int(callback.data.rsplit(":", 1)[1])


async def _add_number_everywhere(message: Message, phone: str) -> None:
    try:
        result = await numbers_store.add_number_to_campaigns(phone)
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось добавить номер {phone} в кампании в БД: {exc}")
        return
    if result.added:
        await message.answer(f"✅ Номер {phone} добавлен в БД для: {'; '.join(result.added)}")


@router.callback_query(F.data == "adm:users")
async def admin_users(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    users = await excel_store.list_users()
    await callback.message.answer_document(
        BufferedInputFile(admin_files.build_users_file(users), filename=f"users_{_date_token()}.xlsx"),
        caption=(
            "Таблицу можно исправить и загрузить обратно кнопкой «📥 Загрузить исправленный Excel».\n"
            "Строка удалена — пользователь удалится; is_admin = «да» — админ."
        ),
    )
    await _show_users(callback.message)


# Возврат к списку из карточки пользователя — без повторной отправки Excel
@router.callback_query(F.data == "adm:users_list")
async def admin_users_list(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _show_users(callback.message)


@router.callback_query(F.data.startswith("adm:user:"))
async def admin_user_card(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_user(callback.message, await _user_id_from_callback(callback))


@router.callback_query(F.data.startswith("adm:uname:"))
async def admin_user_rename(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = await _user_id_from_callback(callback)
    await state.set_state(AdminInput.user_name)
    await state.update_data(edit_user=telegram_id)
    await callback.message.answer("Введите новое имя:", reply_markup=CANCEL_KB)


@router.message(AdminInput.user_name)
async def admin_user_rename_input(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым:", reply_markup=CANCEL_KB)
        return
    telegram_id = (await state.get_data())["edit_user"]
    await state.clear()
    if await _safe_excel(message, excel_store.update_user(telegram_id, name=name)):
        await message.answer("✅ Имя изменено.")
    await _show_user(message, telegram_id)


@router.callback_query(F.data.startswith("adm:uphone:"))
async def admin_user_phone(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = await _user_id_from_callback(callback)
    await state.set_state(AdminInput.user_phone)
    await state.update_data(edit_user=telegram_id)
    await callback.message.answer(
        "Введите новый номер без плюса и пробелов (например 998901234567):", reply_markup=CANCEL_KB
    )


@router.message(AdminInput.user_phone)
async def admin_user_phone_input(message: Message, state: FSMContext) -> None:
    phone, error = validate_phone(message.text or "")
    if error:
        await message.answer(f"❌ {error}\nВведите номер ещё раз:", reply_markup=CANCEL_KB)
        return
    telegram_id = (await state.get_data())["edit_user"]
    await state.clear()
    if await _safe_excel(message, excel_store.update_user(telegram_id, phone=phone)):
        await message.answer("✅ Номер изменён.")
        await _add_number_everywhere(message, phone)
    await _show_user(message, telegram_id)


@router.callback_query(F.data.startswith("adm:uadm:"))
async def admin_user_toggle_admin(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = await _user_id_from_callback(callback)
    if telegram_id == callback.from_user.id:
        await callback.message.answer("Снять админа с самого себя нельзя.")
        await _show_user(callback.message, telegram_id)
        return
    user = next((u for u in await excel_store.list_users() if u["telegram_id"] == telegram_id), None)
    if user is not None and await _safe_excel(
        callback.message, excel_store.update_user(telegram_id, is_admin=not user["is_admin"])
    ):
        access.reset_cache()
    await _show_user(callback.message, telegram_id)


@router.callback_query(F.data.startswith("adm:udel:"))
async def admin_user_delete(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = await _user_id_from_callback(callback)
    if telegram_id == callback.from_user.id:
        await callback.message.answer("Удалить самого себя нельзя.")
        await _show_user(callback.message, telegram_id)
        return
    await callback.message.answer(
        "Удалить пользователя? Его звонки в истории останутся, а чтобы вернуться в бот, "
        "ему придётся заново ввести пароль и зарегистрироваться.",
        reply_markup=_kb(("✅ Да, удалить", f"adm:udelyes:{telegram_id}"), ("↩️ Отмена", f"adm:user:{telegram_id}")),
    )


@router.callback_query(F.data.startswith("adm:udelyes:"))
async def admin_user_delete_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = await _user_id_from_callback(callback)
    if await _safe_excel(callback.message, excel_store.delete_user(telegram_id)):
        access.reset_cache()
        await callback.message.answer("Пользователь удалён.")
    await _show_users(callback.message)


@router.callback_query(F.data == "adm:users_up")
async def admin_users_upload(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(AdminInput.users_file)
    await callback.message.answer(
        "Пришлите таблицу пользователей (.xlsx или .csv) — лучше всего исправленную выгрузку.\n"
        "Колонки: telegram_id, name, phone, is_admin (необязательно). "
        "Таблица заменит текущую целиком — перед этим покажу, что изменится.",
        reply_markup=CANCEL_KB,
    )


@router.message(AdminInput.users_file, F.document)
async def admin_users_file(message: Message, state: FSMContext) -> None:
    filename = message.document.file_name or ""
    if not filename.lower().endswith((".xlsx", ".xlsm", ".csv")):
        await message.answer("Нужен файл .xlsx или .csv.", reply_markup=CANCEL_KB)
        return
    users, errors = admin_files.parse_users_file(await _download(message), filename)
    if errors:
        await message.answer(
            "В файле есть проблемы:\n" + "\n".join(f"• {e}" for e in errors) + "\n\nИсправьте и пришлите ещё раз.",
            reply_markup=CANCEL_KB,
        )
        return
    me = message.from_user.id
    if not access.is_env_admin(me) and not any(u["telegram_id"] == me and u["is_admin"] for u in users):
        await message.answer(
            "В файле вы не админ (или вас нет) — после загрузки вы потеряете доступ к админке. "
            "Исправьте и пришлите ещё раз.",
            reply_markup=CANCEL_KB,
        )
        return

    current = {u["telegram_id"]: u for u in await excel_store.list_users()}
    new = {u["telegram_id"]: u for u in users}
    added = [u for tid, u in new.items() if tid not in current]
    removed = [u for tid, u in current.items() if tid not in new]
    changed = [
        u for tid, u in new.items()
        if tid in current and any(u[k] != current[tid][k] for k in ("name", "phone", "is_admin"))
    ]
    for u in users:
        # Дата регистрации не теряется, даже если колонку из файла убрали
        u["registered_at"] = (
            current[u["telegram_id"]]["registered_at"] if u["telegram_id"] in current and not u["registered_at"]
            else u["registered_at"] or dt.datetime.now().isoformat(timespec="seconds")
        )
    await state.update_data(pending_users=users)

    def names(items: list[dict]) -> str:
        return ", ".join(u["name"] for u in items[:20]) + (" …" if len(items) > 20 else "")

    lines = [f"Пользователей в файле: {len(users)}"]
    if added:
        lines.append(f"➕ Новые ({len(added)}): {names(added)}")
    if changed:
        lines.append(f"✏️ Изменятся ({len(changed)}): {names(changed)}")
    if removed:
        lines.append(f"🗑 Удалятся ({len(removed)}): {names(removed)}")
    if not (added or changed or removed):
        lines.append("Изменений нет.")
    await message.answer(
        "\n".join(lines), reply_markup=_kb(("✅ Применить", "adm:users_apply"), ("↩️ Отмена", "adm:cancel"))
    )


@router.callback_query(F.data == "adm:users_apply")
async def admin_users_apply(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    users = (await state.get_data()).get("pending_users")
    await state.clear()
    if not users:
        await callback.message.answer("Нечего применять — загрузите файл заново.")
        return
    old_phones = {(u["telegram_id"], u["phone"]) for u in await excel_store.list_users()}
    if await _safe_excel(callback.message, excel_store.replace_users(users)) is None:
        return
    access.reset_cache()
    await callback.message.answer("✅ Таблица пользователей обновлена.")
    for u in users:
        if (u["telegram_id"], u["phone"]) not in old_phones:
            await _add_number_everywhere(callback.message, u["phone"])
    await _show_users(callback.message)


# --- Логи звонков ---

# Поиск по названию: больше совпадений — просим уточнить, чтобы не выгружать полбазы
SEARCH_LIMIT = 30
# Сколько ждать отправки одной части архива: до 49 МБ, через прокси бывает медленно
ARCHIVE_UPLOAD_TIMEOUT = 600


async def _send_calls_archives(message: Message, records: list[dict], name: str, title: str) -> None:
    """Логи, транскрипции и аудио выбранных звонков одним архивом или несколькими частями
    (каждая не больше лимита Telegram). Части собираются и отправляются по очереди."""
    await message.answer(f"⏳ Собираю архив: {title}, звонков: {len(records)}...")
    plan = await asyncio.to_thread(export_service.plan_calls_archives, records)
    if plan is None:
        await message.answer("Файлов и текста логов по этим звонкам нет.")
        return
    total = len(plan.parts)
    if total > 1:
        await message.answer(f"Архив большой — пришлю его частями: {total} шт.")
    failed = []
    for i in range(total):
        suffix = f"_part{i + 1}of{total}" if total > 1 else ""
        caption = f"📦 {title}" + (f" (часть {i + 1} из {total})" if total > 1 else "")
        try:
            data = await asyncio.to_thread(plan.build, i)
            # message.answer_document не пробрасывает request_timeout, поэтому через bot
            await message.bot.send_document(
                chat_id=message.chat.id, document=BufferedInputFile(data, filename=f"{name}{suffix}.zip"),
                caption=caption, request_timeout=ARCHIVE_UPLOAD_TIMEOUT,
            )
        except Exception as exc:
            failed.append(f"часть {i + 1}: {exc}")
    if failed:
        await message.answer("⚠️ Не отправилось:\n" + "\n".join(failed))
    await message.answer("Готово.", reply_markup=_kb(("↩️ Логи звонков", "adm:logs"), ("↩️ Админка", "menu:admin")))


@router.callback_query(F.data == "adm:logs")
async def admin_logs(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    rows = [(c.title, f"adm:logc:{c.key}") for c in campaigns.all_campaigns()]
    rows += [("🔎 Найти по названию звонка (call_id / sid)", "adm:logsearch"), ("↩️ Админка", "menu:admin")]
    await callback.message.answer("Логи звонков: выберите кампанию или найдите звонок по названию.", reply_markup=_kb(*rows))


@router.callback_query(F.data.startswith("adm:logc:"))
async def admin_logs_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    campaign = await _campaign_from_callback(callback)
    if campaign is None:
        return
    users = await excel_store.call_users(campaign.title)
    if not users:
        await callback.message.answer(
            f"По «{campaign.title}» звонков ещё не было.", reply_markup=_kb(("↩️ Логи звонков", "adm:logs"))
        )
        return
    rows = [("👥 Все пользователи", f"adm:logu:{campaign.key}:0")]
    rows += [(name, f"adm:logu:{campaign.key}:{tid}") for tid, name in users[:_MAX_LIST_BUTTONS]]
    rows.append(("↩️ Логи звонков", "adm:logs"))
    await callback.message.answer(f"«{campaign.title}»: чьи логи прислать?", reply_markup=_kb(*rows))


@router.callback_query(F.data.startswith("adm:logu:"))
async def admin_logs_user(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    _, _, key, tid = callback.data.split(":")
    campaign = campaigns.get(key)
    if campaign is None:
        await callback.message.answer("Кампания не найдена — возможно, её уже удалили.")
        return
    telegram_id = int(tid) or None
    records = await excel_store.find_calls(campaign.title, telegram_id, limit=10**9)
    if not records:
        await callback.message.answer("Звонков не найдено.", reply_markup=_kb(("↩️ Логи звонков", "adm:logs")))
        return
    who = "все" if telegram_id is None else str(records[0].get("name") or telegram_id)
    name = f"logs_{_sanitize_folder_name(campaign.title)}_{_sanitize_folder_name(who)}_{_date_token()}"
    await _send_calls_archives(callback.message, records, name, f"«{campaign.title}», {who}")


@router.callback_query(F.data == "adm:logsearch")
async def admin_logs_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(AdminInput.log_search)
    await callback.message.answer(
        "Введите название звонка (call_id или sid) целиком или его часть — так называются файлы логов.",
        reply_markup=CANCEL_KB,
    )


@router.message(AdminInput.log_search)
async def admin_logs_search_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 4:
        await message.answer("Введите хотя бы 4 символа:", reply_markup=CANCEL_KB)
        return
    records = await excel_store.find_calls(text=text, limit=SEARCH_LIMIT + 1)
    if not records:
        await message.answer("Звонков с таким названием не найдено — попробуйте ещё раз:", reply_markup=CANCEL_KB)
        return
    if len(records) > SEARCH_LIMIT:
        await message.answer(
            f"Под «{text}» подходит больше {SEARCH_LIMIT} звонков — уточните название:", reply_markup=CANCEL_KB
        )
        return
    await state.clear()
    name = f"logs_{_sanitize_folder_name(text)}_{_date_token()}"
    await _send_calls_archives(message, records, name, f"поиск «{text}»")


# Должен идти после обработчиков файлов: ловит текст/фото вместо документа
@router.message(StateFilter(AdminInput.var_list_file, AdminInput.testcases_file, AdminInput.users_file))
async def admin_wrong_file_type(message: Message, state: FSMContext) -> None:
    await message.answer("Пришлите файл как документ (не текстом и не фото).", reply_markup=CANCEL_KB)
