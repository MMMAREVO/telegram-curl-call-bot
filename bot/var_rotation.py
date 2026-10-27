import hashlib
import json
import random
from pathlib import Path

from . import config
from .campaigns import Campaign

# Строки списка переменных выдаются каждому пользователю в случайном порядке без повторов;
# когда круг пройден (или админ загрузил новый список) — список перемешивается заново.
# Прогресс хранится в JSON, чтобы переживать перезапуск бота.


def _signature(campaign: Campaign) -> str:
    raw = json.dumps([campaign.var_list.columns, campaign.var_list.rows], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load_state() -> dict:
    try:
        return json.loads(Path(config.VAR_ROTATION_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    Path(config.VAR_ROTATION_PATH).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def peek_next(telegram_id: int, campaign: Campaign) -> tuple[int, dict[str, str]]:
    """Следующая строка списка в круге пользователя: (номер строки, значения переменных).
    Из очереди не убирается — см. commit. Список должен быть пригоден (var_list_problem)."""
    signature = _signature(campaign)
    state = _load_state()
    state_key = f"{campaign.key}:{telegram_id}"
    user_state = state.get(state_key) or {}

    if user_state.get("signature") != signature or not user_state.get("queue"):
        queue = list(range(len(campaign.var_list.rows)))
        random.shuffle(queue)
        user_state = state[state_key] = {"signature": signature, "queue": queue}
        _save_state(state)

    row_index = user_state["queue"][0]
    row = dict(zip(campaign.var_list.columns, campaign.var_list.rows[row_index]))
    return row_index, {name: row[name] for name in campaign.variables}


def commit(telegram_id: int, campaign: Campaign, row_index: int) -> None:
    """Строка использована (звонок запущен) — убираем её из очереди пользователя."""
    state = _load_state()
    queue = (state.get(f"{campaign.key}:{telegram_id}") or {}).get("queue") or []
    if row_index in queue:
        queue.remove(row_index)
        _save_state(state)
