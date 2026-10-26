import hashlib
import json
import random
from pathlib import Path

from . import config

# Модели для Узум авто: строки "Марка Модель" из файла. Каждому пользователю выдаются
# в случайном порядке без повторов; когда круг пройден — список перемешивается заново.
# Прогресс хранится в JSON, чтобы переживать перезапуск бота.


class CarModelsError(Exception):
    pass


def parse_car(text: str) -> tuple[str, str] | None:
    """'Chevrolet Captiva Sport' -> ('chevrolet', 'captiva sport'): марка — первое слово."""
    parts = " ".join(text.split()).lower().split(" ", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _load_models() -> list[str]:
    path = Path(config.CAR_MODELS_PATH)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise CarModelsError(f"не удалось прочитать {path}: {exc}") from exc

    models: list[str] = []
    for line in lines:
        car = parse_car(line)
        if car is None:
            continue
        item = " ".join(car)
        if item not in models:
            models.append(item)
    if not models:
        raise CarModelsError(f"в файле {path} нет строк вида «Марка Модель»")
    return models


def _signature(models: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(models)).encode("utf-8")).hexdigest()


def _load_state() -> dict:
    try:
        return json.loads(Path(config.CAR_ROTATION_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    Path(config.CAR_ROTATION_PATH).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def peek_next(telegram_id: int) -> tuple[str, str]:
    """Следующая модель в круге пользователя (из очереди не убирается — см. commit)."""
    models = _load_models()
    signature = _signature(models)
    state = _load_state()
    user_state = state.get(str(telegram_id)) or {}

    # Новый круг: очередь пуста или файл со списком изменился
    if user_state.get("signature") != signature or not user_state.get("queue"):
        queue = models[:]
        random.shuffle(queue)
        state[str(telegram_id)] = {"signature": signature, "queue": queue}
        _save_state(state)
        user_state = state[str(telegram_id)]

    return parse_car(user_state["queue"][0])


def commit(telegram_id: int, brand: str, model: str) -> None:
    """Модель использована (звонок запущен) — убираем её из очереди пользователя."""
    state = _load_state()
    queue = (state.get(str(telegram_id)) or {}).get("queue") or []
    item = f"{brand} {model}"
    if item in queue:
        queue.remove(item)
        _save_state(state)
