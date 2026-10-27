import re

PHONE_RE = re.compile(r"^\d{7,15}$")


def validate_phone(raw: str) -> tuple[str | None, str | None]:
    """Проверяет номер, введённый пользователем. Возвращает (номер, None) или (None, текст ошибки)."""
    text = raw.strip()
    if not text:
        return None, "Номер пустой."
    if " " in text or "\t" in text:
        return None, "В номере не должно быть пробелов."
    if "+" in text:
        return None, "Номер вводится без плюса."
    if not text.isdigit():
        return None, "Номер должен состоять только из цифр (без скобок, дефисов и букв)."
    if text.startswith("998") and len(text) != 12:
        return None, f"Номер Узбекистана — 12 цифр (998 + 9 цифр), а введено {len(text)}."
    if text.startswith("7") and len(text) != 11:
        return None, f"Номер России — 11 цифр (7 + 10 цифр), а введено {len(text)}."
    if not PHONE_RE.match(text):
        return None, "Номер должен содержать от 7 до 15 цифр."
    return text, None
