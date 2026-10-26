@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === Установка бота: создание виртуального окружения (.venv) ===
echo.

rem Ищем Python: сначала лаунчер py, потом python из PATH
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ОШИБКА] Python не найден.
    echo Установите Python 3.11+ с https://www.python.org/downloads/
    echo и при установке отметьте галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Найден Python:
%PY% --version
echo.

if exist ".venv\Scripts\python.exe" (
    echo Папка .venv уже есть — использую её.
) else (
    echo Создаю .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось создать .venv
        pause
        exit /b 1
    )
)

echo.
echo Обновляю pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo.
echo Устанавливаю зависимости из requirements.txt ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Зависимости не установились. Проверьте интернет / прокси и запустите setup.bat ещё раз.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo [!] Файл .env не найден — создан из .env.example. Заполните его перед запуском.
)

echo.
echo === Готово! Проверьте .env и запускайте бота через start.bat ===
pause
