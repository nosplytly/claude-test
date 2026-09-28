@echo off
chcp 65001 >nul
rem Local run: double-click. Stop it with Ctrl+C or by closing this window.
cd /d "%~dp0"
set "PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do set "PID=%%p"
if defined PID (
  echo Порт 8000 занят процессом PID %PID% — скорее всего, это старый локальный сервер.
  choice /m "Остановить его"
  if errorlevel 2 exit /b 1
  taskkill /PID %PID% /F >nul || goto :nokill
  timeout /t 2 /nobreak >nul
)
if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: ставлю зависимости...
  python -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :nopython
)
echo Сайт: http://localhost:8000  ^(бот запускается вместе с ним^)
start "" http://localhost:8000
".venv\Scripts\python.exe" run.py
pause
exit /b 0
:nokill
echo Не получилось остановить процесс %PID%. Закрой старый сервер вручную и запусти этот файл снова.
pause
exit /b 1
:nopython
echo Не получилось подготовить Python. Нужен Python 3.11+ ^(python.org^).
pause
exit /b 1
