import json
import re

# Строка реплики пользователя в транскрипции из БД: "[User]текст"
_USER_LINE = re.compile(r"^\[User\](.*)$")


def _iter_json_objects(log_text: str):
    """Логи CPAAS — по одному JSON-объекту на строку (иногда с префиксом), всё прочее пропускаем."""
    decoder = json.JSONDecoder()
    for line in log_text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            obj, _ = decoder.raw_decode(line, start)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _execution_steps(log_text: str) -> list[dict]:
    """Шаги сценария из лога. Берём запись с самым полным списком шагов — итоговая
    запись звонка (do_stop) содержит их все."""
    best: list[dict] = []
    for obj in _iter_json_objects(log_text):
        steps = (obj.get("text_data") or {}).get("execution_steps") or []
        if len(steps) > len(best):
            best = steps
    return sorted(best, key=lambda s: s.get("ord", 0))


def _phrase_from_request(step: dict) -> str | None:
    body = (step.get("properties") or {}).get("body") or ""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    phrase = data.get("phrase") if isinstance(data, dict) else None
    return phrase if isinstance(phrase, str) else None


def extract_classifications(log_text: str) -> list[tuple[str, str]]:
    """Пары (фраза пользователя, интент) в порядке звонка.

    Фраза — тело запроса в классификатор (make-http-request с {"phrase": ...}),
    интент — input следующего за ним шага ветвления *_classifier_response."""
    result: list[tuple[str, str]] = []
    pending: str | None = None
    for step in _execution_steps(log_text):
        step_type = step.get("type")
        if step_type == "make-http-request":
            phrase = _phrase_from_request(step)
            if phrase is not None:
                pending = phrase
        elif step_type == "split-based-on" and pending is not None:
            name = (step.get("name") or "").lower()
            if "classif" in name and "response" in name:
                intent = (step.get("properties") or {}).get("input")
                if intent not in (None, ""):
                    result.append((pending, str(intent)))
                pending = None
    return result


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def annotate_transcript(transcript: str, log_texts: list[str]) -> str:
    """Дописывает к каждой непустой реплике [User] определённый интент: "[User]да (yes)".

    Реплики сопоставляются с фразами классификатора по тексту и по порядку. Реплики,
    которые в классификатор не уходили (например, приветствие), остаются без интента."""
    classifications: list[tuple[str, str]] = []
    for log_text in log_texts:
        found = extract_classifications(log_text)
        if len(found) > len(classifications):
            classifications = found
    if not classifications:
        return transcript

    pos = 0
    lines = []
    for line in transcript.splitlines():
        m = _USER_LINE.match(line)
        text = m.group(1).strip() if m else ""
        if text:
            target = _normalize(text)
            for i in range(pos, len(classifications)):
                phrase, intent = classifications[i]
                if _normalize(phrase) == target:
                    line = f"{line.rstrip()} (intent - {intent})"
                    pos = i + 1
                    break
        lines.append(line)
    return "\n".join(lines)
