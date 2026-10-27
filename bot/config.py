import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Пароль, который бот спрашивает у новых пользователей перед регистрацией.
# Обязателен: без него бот открыт любому, кто найдёт его в Telegram.
ACCESS_PASSWORD = os.environ.get("BOT_ACCESS_PASSWORD", "")
if not ACCESS_PASSWORD:
    raise RuntimeError("В .env не задан BOT_ACCESS_PASSWORD — пароль для доступа к боту")
# Telegram может быть недоступен напрямую — если задан HTTPS_PROXY/HTTP_PROXY (или BOT_PROXY),
# aiogram будет ходить в Telegram через него.
BOT_PROXY = os.getenv("BOT_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""

# Адрес API кампаний — нужен только при первом запуске, чтобы собрать курлы встроенных
# кампаний. Дальше курлы целиком (с адресом) хранятся в CAMPAIGNS_PATH и правятся из бота.
CAMPAIGNS_API_BASE = os.getenv("CAMPAIGNS_API_BASE", "")
CAMPAIGNS_PATH = os.getenv("CAMPAIGNS_PATH", "campaigns.json")
EXCEL_PATH = os.getenv("EXCEL_PATH", "bot_data.xlsx")
LOGS_DIR = os.getenv("LOGS_DIR", "call_logs")
# Старый список "Марка Модель" для Узум авто: при первом запуске переносится в список
# переменных кампании (brand/model)
CAR_MODELS_PATH = os.getenv("CAR_MODELS_PATH", "model_marka.txt")
# Прогресс ротации строк списков переменных по пользователям
VAR_ROTATION_PATH = os.getenv("VAR_ROTATION_PATH", "var_rotation.json")

# Telegram ID администраторов (через запятую) — им доступна админка и выгрузка данных всех
# исполнителей; остальные видят в экспорте только свои звонки. Ещё админов можно назначить
# из бота (колонка is_admin листа Users); админов из .env из бота снять нельзя.
ADMIN_TELEGRAM_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()
}

# PostgreSQL — где хранится транскрипция разговора
PG_HOST = os.getenv("PG_HOST", "")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "")
PG_USER = os.getenv("PG_USER", "")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_TABLE = os.getenv("PG_TABLE", "")
PG_TEXT_COLUMN = os.getenv("PG_TEXT_COLUMN", "transcribation")

KUBECTL_BIN = os.getenv("KUBECTL_BIN", "kubectl")


@dataclass(frozen=True)
class LogSource:
    label: str
    host: str
    port: int
    user: str
    key_path: str
    password: str
    sudo_password: str
    namespace: str
    pod: str


# Логи звонка живут в двух местах — ЛАБА (iba-app) и CPAAS (cpaas-studio).
# SSH2_* по умолчанию наследуют логин/ключ первого сервера, если не заданы отдельно.
LOG_SOURCES = [
    LogSource(
        label="iba-app (ЛАБА)",
        host=os.getenv("SSH_HOST", ""),
        port=int(os.getenv("SSH_PORT", "22")),
        user=os.getenv("SSH_USER", ""),
        key_path=os.getenv("SSH_KEY_PATH", ""),
        password=os.getenv("SSH_PASSWORD", ""),
        sudo_password=os.getenv("SSH_SUDO_PASSWORD", ""),
        namespace=os.getenv("K8S_NAMESPACE", "iba"),
        pod=os.getenv("K8S_POD", "iba-app-0"),
    ),
    LogSource(
        label="cpaas-studio (CPAAS)",
        host=os.getenv("SSH2_HOST", ""),
        port=int(os.getenv("SSH2_PORT", "22")),
        user=os.getenv("SSH2_USER") or os.getenv("SSH_USER", ""),
        key_path=os.getenv("SSH2_KEY_PATH") or os.getenv("SSH_KEY_PATH", ""),
        password=os.getenv("SSH2_PASSWORD", ""),
        sudo_password=os.getenv("SSH2_SUDO_PASSWORD", ""),
        namespace=os.getenv("K8S_NAMESPACE_2", "cpaas"),
        pod=os.getenv("K8S_POD_2", "cpaas-studio-0"),
    ),
]

# VictoriaLogs — общее хранилище логов CPAAS (asr-orch, sfas, sengine, vad, text-lid...).
# Пусто — сбор логов оттуда и расчёт паузы для собеседника отключены.
VLOGS_URL = os.getenv("VLOGS_URL", "").rstrip("/")
# Запас по времени вокруг звонка при поиске его логов
VLOGS_WINDOW_PADDING_SECONDS = int(os.getenv("VLOGS_WINDOW_PADDING_SECONDS", "60"))

# Опрос БД: как часто проверять и сколько максимум ждать завершения звонка / транскрипции.
# Реальный разговор с IVR (меню, повторы вопроса) может идти несколько минут, поэтому лимиты щедрые.
CALL_POLL_INTERVAL = float(os.getenv("CALL_POLL_INTERVAL", "3"))
CALL_COMPLETION_TIMEOUT = float(os.getenv("CALL_COMPLETION_TIMEOUT", "240"))
TRANSCRIPT_TIMEOUT = float(os.getenv("TRANSCRIPT_TIMEOUT", "120"))
# Как часто присылать в чат "всё ещё жду" во время ожидания
PROGRESS_PING_SECONDS = float(os.getenv("PROGRESS_PING_SECONDS", "30"))

# Запас в секундах, добавляемый к окну "--since" при запросе логов через kubectl
LOG_FETCH_BUFFER_SECONDS = int(os.getenv("LOG_FETCH_BUFFER_SECONDS", "15"))

# Жёсткий потолок на всю обработку одного звонка (ожидание БД + логи + транскрипция).
# Звонок физически не может идти дольше 15 минут — если обработка зависла (например,
# на SSH- или БД-подключении), прерываем сессию в Telegram, а не висим бесконечно.
MAX_CALL_SESSION_SECONDS = float(os.getenv("MAX_CALL_SESSION_SECONDS", str(15 * 60)))
