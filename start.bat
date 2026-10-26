@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Виртуальное окружение не найдено. Сначала запустите setup.bat
    pause
    exit /b 1
)

echo Бот запущен. Чтобы остановить — закройте это окно или нажмите Ctrl+C.
echo.
".venv\Scripts\python.exe" -m bot.main
pause
