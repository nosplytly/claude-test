@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Собирает dist\SupplierHub\SupplierHub.exe. Нужен Python 3.11+ (python.org, галочка "Add to PATH").

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
    echo Создаю окружение .venv ...
    %PY% -m venv .venv || goto :error
)

echo Ставлю зависимости ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt || goto :error

echo Собираю SupplierHub.exe ...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean SupplierHub.spec || goto :error

echo.
echo Готово: dist\SupplierHub\SupplierHub.exe
echo Папку dist\SupplierHub можно перенести куда угодно - настройки и склад хранятся рядом с exe.
pause
exit /b 0

:error
echo.
echo Сборка не удалась - смотрите сообщения выше.
pause
exit /b 1
