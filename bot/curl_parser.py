import copy
import json
import re
import shlex
import uuid
from dataclasses import dataclass

# Переменная в теле курла: "brand": "{{brand}}". Значение подставляется при звонке —
# из списка переменных кампании или вручную.
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_CAMPAIGN_ID_RE = re.compile(r"/campaigns/([^/?#]+)/calls")

_DATA_OPTS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"}
_HEADER_OPTS = {"-H", "--header"}
_METHOD_OPTS = {"-X", "--request"}
# Ключи без аргумента, которые на запрос не влияют
_IGNORED_FLAGS = {
    "-L", "--location", "-s", "--silent", "-S", "--show-error", "-k", "--insecure",
    "-v", "--verbose", "-i", "--include", "--compressed", "-g", "--globoff",
}
# Заголовки, которые httpx выставит сам
_DROPPED_HEADERS = {"content-length", "host"}


class CurlError(Exception):
    pass


@dataclass
class ParsedCurl:
    url: str
    campaign_id: str
    headers: dict[str, str]
    body: dict


def _normalize(text: str) -> str:
    # Телефоны любят "умные" кавычки, а bash/cmd — переносы строк через \ и ^
    for smart, plain in (("“", '"'), ("”", '"'), ("„", '"'), ("‘", "'"), ("’", "'")):
        text = text.replace(smart, plain)
    return re.sub(r"[\\^][ \t]*\r?\n", " ", text).strip()


def parse_curl(text: str) -> ParsedCurl:
    try:
        tokens = shlex.split(_normalize(text))
    except ValueError as exc:
        raise CurlError(f"не удалось разобрать команду (проверьте кавычки): {exc}") from exc
    if not tokens or tokens[0] != "curl":
        raise CurlError("текст должен начинаться с curl")

    url = ""
    headers: dict[str, str] = {}
    data: str | None = None
    it = iter(tokens[1:])
    for token in it:
        if token in _DATA_OPTS or token in _HEADER_OPTS or token in _METHOD_OPTS or token == "--url":
            value = next(it, None)
            if value is None:
                raise CurlError(f"после {token} нет значения")
            if token in _DATA_OPTS:
                data = value
                if token == "--json":
                    headers.setdefault("Content-Type", "application/json")
            elif token in _HEADER_OPTS:
                name, sep, header_value = value.partition(":")
                if not sep:
                    raise CurlError(f"заголовок без двоеточия: {value}")
                if name.strip().lower() not in _DROPPED_HEADERS:
                    headers[name.strip()] = header_value.strip()
            elif token in _METHOD_OPTS:
                if value.upper() != "POST":
                    raise CurlError(f"поддерживается только POST, а в курле {value}")
            else:
                url = value
        elif token in _IGNORED_FLAGS:
            continue
        elif token.startswith("-"):
            raise CurlError(f"неизвестный ключ {token}")
        else:
            url = token

    if not url:
        raise CurlError("в курле нет адреса")
    match = _CAMPAIGN_ID_RE.search(url)
    if not match:
        raise CurlError("адрес должен быть вида …/campaigns/<id кампании>/calls")
    try:
        campaign_id = str(uuid.UUID(match.group(1)))
    except ValueError as exc:
        raise CurlError(f"id кампании в адресе не похож на UUID: {match.group(1)}") from exc

    if data is None:
        raise CurlError("в курле нет тела запроса (-d / --data)")
    try:
        body = json.loads(data)
    except ValueError as exc:
        raise CurlError(f"тело запроса — не JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise CurlError("тело запроса должно быть JSON-объектом {...}")
    if "number" not in body:
        raise CurlError("в теле запроса нет поля number — туда бот подставляет номер исполнителя")

    return ParsedCurl(url=url, campaign_id=campaign_id, headers=headers, body=body)


def _walk_strings(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _walk_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk_strings(v, fn) for v in value]
    return value


def find_variables(body: dict) -> list[str]:
    """Имена переменных {{...}} из тела в порядке появления, без повторов."""
    found: list[str] = []

    def collect(s: str) -> str:
        for name in VAR_RE.findall(s):
            if name not in found:
                found.append(name)
        return s

    _walk_strings(body, collect)
    return found


def render_body(body: dict, phone: str, values: dict[str, str]) -> dict:
    """Тело для отправки: номер исполнителя в number и значения вместо {{переменных}}."""
    rendered = _walk_strings(copy.deepcopy(body), lambda s: VAR_RE.sub(lambda m: values[m.group(1)], s))
    rendered["number"] = phone
    return rendered


def render_curl(url: str, headers: dict[str, str], body: dict) -> str:
    parts = [f"curl -X POST {shlex.quote(url)}"]
    parts += [f"-H {shlex.quote(f'{k}: {v}')}" for k, v in headers.items()]
    parts.append(f"-d {shlex.quote(json.dumps(body, ensure_ascii=False))}")
    return " \\\n  ".join(parts)
