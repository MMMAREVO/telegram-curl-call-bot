import dataclasses
import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config
from .curl_parser import ParsedCurl, find_variables

# Кампании (курлы) и их списки переменных хранятся в JSON и правятся админом из бота.
# При первом запуске файл заполняется кампаниями, которые раньше были прописаны в коде.


@dataclass
class VariableList:
    columns: list[str]
    rows: list[list[str]]


@dataclass
class Campaign:
    key: str
    title: str
    url: str
    campaign_id: str
    headers: dict[str, str]
    # JSON-тело запроса: в number подставляется номер исполнителя, вместо {{имя}} — переменные
    body: dict
    var_list: VariableList | None = None

    @property
    def variables(self) -> list[str]:
        return find_variables(self.body)

    def var_list_problem(self) -> str | None:
        """Почему список переменных нельзя использовать для звонка (None — можно)."""
        if self.var_list is None or not self.var_list.rows:
            return "список переменных не загружен"
        missing = [v for v in self.variables if v not in self.var_list.columns]
        if missing:
            return f"в списке переменных нет колонок: {', '.join(missing)}"
        return None


_DEFAULT_CAMPAIGNS = [
    ("microloans_ru", "Микрозаймы (RU)", "9a8b7c6d-5e4f-3120-8b1c-2d3e4f5a6b7c", {"client_lang": "ru"}),
    ("microloans_uz", "Микрозаймы (UZ)", "9a8b7c6d-5e4f-3120-8b1c-2d3e4f5a6b7c", {"client_lang": "uz"}),
    ("limits_ru", "Лимиты (RU)", "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a", {"client_lang": "ru"}),
    ("limits_uz", "Лимиты (UZ)", "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a", {"client_lang": "uz"}),
    ("uzum_auto", "Узум авто", "b3c4d5e6-f7a8-4901-9b2c-3d4e5f6a7b8c", {"brand": "{{brand}}", "model": "{{model}}"}),
    ("credits", "Кредиты", "dd426938-5558-4e09-9fd1-d039e11e4bca", {}),
]


def _legacy_car_list() -> VariableList | None:
    """Старый model_marka.txt ("Марка Модель" в строке) -> список brand/model для Узум авто."""
    try:
        lines = Path(config.CAR_MODELS_PATH).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    rows: list[list[str]] = []
    for line in lines:
        parts = " ".join(line.split()).lower().split(" ", 1)
        if len(parts) == 2 and parts not in rows:
            rows.append(parts)
    return VariableList(columns=["brand", "model"], rows=rows) if rows else None


def _seed() -> dict[str, Campaign]:
    base = config.CAMPAIGNS_API_BASE.rstrip("/")
    campaigns = {
        key: Campaign(
            key=key, title=title, url=f"{base}/campaigns/{campaign_id}/calls", campaign_id=campaign_id,
            headers={"Content-Type": "application/json"}, body={"number": "", "params": params},
        )
        for key, title, campaign_id, params in _DEFAULT_CAMPAIGNS
    }
    campaigns["uzum_auto"].var_list = _legacy_car_list()
    return campaigns


def _from_dict(raw: dict) -> Campaign:
    var_list = VariableList(**raw["var_list"]) if raw.get("var_list") else None
    return Campaign(**{**raw, "var_list": var_list})


_campaigns: dict[str, Campaign] | None = None


def _save() -> None:
    path = Path(config.CAMPAIGNS_PATH)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps([asdict(c) for c in _campaigns.values()], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def _load() -> dict[str, Campaign]:
    global _campaigns
    if _campaigns is None:
        path = Path(config.CAMPAIGNS_PATH)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            _campaigns = {c["key"]: _from_dict(c) for c in raw}
        else:
            _campaigns = _seed()
            _save()
    return _campaigns


def all_campaigns() -> list[Campaign]:
    return list(_load().values())


def get(key: str) -> Campaign | None:
    return _load().get(key)


def by_title(title: str) -> Campaign | None:
    return next((c for c in _load().values() if c.title == title), None)


def add(title: str, parsed: ParsedCurl) -> Campaign:
    campaigns = _load()
    key = f"c_{secrets.token_hex(4)}"
    campaigns[key] = Campaign(
        key=key, title=title, url=parsed.url, campaign_id=parsed.campaign_id,
        headers=parsed.headers, body=parsed.body,
    )
    _save()
    return campaigns[key]


def replace_curl(key: str, parsed: ParsedCurl) -> Campaign:
    campaigns = _load()
    campaigns[key] = dataclasses.replace(
        campaigns[key], url=parsed.url, campaign_id=parsed.campaign_id,
        headers=parsed.headers, body=parsed.body,
    )
    _save()
    return campaigns[key]


def set_var_list(key: str, var_list: VariableList | None) -> None:
    _load()[key].var_list = var_list
    _save()


def delete(key: str) -> None:
    del _load()[key]
    _save()
