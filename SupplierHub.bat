@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Запуск SupplierHub из исходников, без сборки exe. При первом запуске ставит зависимости.

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Первый запуск: создаю окружение .venv ...
    %PY% -m venv .venv || goto :error
)

".venv\Scripts\python.exe" -c "import webview, tomli_w, PlayerokAPI" >nul 2>nul
if errorlevel 1 (
    echo Ставлю зависимости ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements-gui.txt || goto :error
)

start "" ".venv\Scripts\pythonw.exe" -m supplierhub
exit /b 0

:error
echo.
echo Не удалось запустить SupplierHub. Нужен Python 3.11+ с python.org (галочка "Add to PATH").
pause
exit /b 1
