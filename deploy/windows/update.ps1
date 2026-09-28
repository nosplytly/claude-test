#Requires -RunAsAdministrator
<#
  Обновление после замены файлов проекта (или правки .env):
      powershell -ExecutionPolicy Bypass -File .\deploy\windows\update.ps1
  Данные (data\), .env и certs\ не трогаются.
#>
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "==> зависимости" -ForegroundColor Cyan
& $VenvPy -m pip install --quiet -r (Join-Path $Root "requirements.txt")

Write-Host "==> перезапуск" -ForegroundColor Cyan
Restart-Service supplierhub-app
Restart-Service supplierhub-caddy
Start-Sleep 6
try {
  $h = Invoke-RestMethod "http://127.0.0.1:8000/healthz" -TimeoutSec 10
  Write-Host "    ok, nervixy_error: $($h.nervixy_error)" -ForegroundColor Green
} catch {
  Write-Host "    приложение не отвечает — смотри data\logs\app.log" -ForegroundColor Yellow
}
