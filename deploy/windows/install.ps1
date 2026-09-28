#Requires -RunAsAdministrator
<#
  SupplierHub — установка на Windows VDS (Windows Server 2019+/Windows 10+).

  1. Скопируй папку проекта на сервер, например в C:\supplierhub
  2. Положи сертификат: certs\fullchain.pem и certs\privkey.pem
  3. Заполни .env (образец — .env.example)
  4. PowerShell от администратора:
       cd C:\supplierhub
       powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1

  Скрипт можно запускать повторно — он переустановит службы с текущими настройками.
#>
param(
  [string]$Domain = "sh-control-universe.com",
  [int]$AppPort = 8000
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Bin = Join-Path $PSScriptRoot "bin"
$Logs = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force $Bin, $Logs | Out-Null
$RootFwd = $Root -replace '\\', '/'

function Write-Utf8($path, $text) { [IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false)) }
function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "    $t" -ForegroundColor Green }
function Warn($t) { Write-Host "    $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host "`n[X] $t" -ForegroundColor Red; exit 1 }
# Windows PowerShell 5.1 + "Stop": any stderr line of a native command redirected with 2>&1 aborts the script,
# and caddy logs to stderr even on success (WinSW may too). Run them with a local "Continue"; judge them by $LASTEXITCODE.
function Quiet([scriptblock]$Cmd) { $ErrorActionPreference = "Continue"; & $Cmd 2>&1 | Out-Null }

# ---------------------------------------------------------------- checks
Step "Проверка файлов"
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
  Copy-Item (Join-Path $Root ".env.example") $envFile
  Fail "Создан .env из образца. Заполни его (NERVIXY_API_KEY, BOT_TOKEN, ADMIN_TG_IDS, кошельки) и запусти скрипт снова."
}
$envText = Get-Content $envFile -Raw
if ($envText -notmatch '(?m)^\s*ENV\s*=\s*prod') { Warn "В .env не стоит ENV=prod — сайт запустится в dev-режиме (dev-вход без Telegram!)" }
if ($envText -notmatch '(?m)^\s*NERVIXY_API_KEY\s*=\s*\S+') { Fail "В .env пустой NERVIXY_API_KEY" }
if ($envText -match '(?m)^\s*NERVIXY_MOCK\s*=\s*true') { Warn "NERVIXY_MOCK=true — заказы НЕ будут уходить поставщику" }
if ($envText -notmatch '(?m)^\s*BOT_TOKEN\s*=\s*\S+') { Warn "BOT_TOKEN пустой — вход через Telegram работать не будет" }
if ($envText -notmatch '(?m)^\s*ADMIN_TG_IDS\s*=\s*\d') { Warn "ADMIN_TG_IDS пустой — алерты админу никуда не придут" }
if ($envText -notmatch '(?m)^\s*WALLET_\w+\s*=\s*\S+') { Warn "Не указан ни один кошелёк — оплата криптой будет выключена" }
foreach ($f in @("certs\fullchain.pem", "certs\privkey.pem")) {
  if (-not (Test-Path (Join-Path $Root $f))) { Fail "Нет файла $f" }
}
Ok "ok"

# ---------------------------------------------------------------- python
Step "Python"
function Find-Python {
  foreach ($p in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python313\python.exe")) {
    if (Test-Path $p) { return $p }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notlike "*WindowsApps*") {
    $v = & $cmd.Source -c "import sys; print(sys.version_info >= (3, 11))"
    if ($v -eq "True") { return $cmd.Source }
  }
  return $null
}
$Python = Find-Python
if (-not $Python) {
  Warn "Python не найден — ставлю Python 3.12"
  $inst = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $inst
  Start-Process $inst -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_launcher=1" -Wait
  $Python = Find-Python
  if (-not $Python) { Fail "Не удалось установить Python" }
}
Ok $Python

$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { & $Python -m venv $Venv }
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install не прошёл" }
Ok "зависимости установлены"

# ---------------------------------------------------------------- ports
Step "Порты 80/443"
$busy = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 80, 443 }
foreach ($b in $busy) {
  $proc = Get-Process -Id $b.OwningProcess -ErrorAction SilentlyContinue
  if ($proc.ProcessName -notin @("caddy", "supplierhub-caddy")) {
    Warn "Порт $($b.LocalPort) занят процессом $($proc.ProcessName) (PID $($b.OwningProcess)). Если это IIS — останови: Stop-Service W3SVC; Set-Service W3SVC -StartupType Disabled"
  }
}

# ---------------------------------------------------------------- binaries
Step "Caddy и WinSW"
$caddy = Join-Path $Bin "caddy.exe"
if (-not (Test-Path $caddy)) {
  $rel = Invoke-RestMethod "https://api.github.com/repos/caddyserver/caddy/releases/latest" -Headers @{ "User-Agent" = "supplierhub-install" }
  $asset = $rel.assets | Where-Object { $_.name -like "caddy_*_windows_amd64.zip" } | Select-Object -First 1
  $zip = Join-Path $env:TEMP $asset.name
  Invoke-WebRequest $asset.browser_download_url -OutFile $zip
  Expand-Archive $zip -DestinationPath (Join-Path $env:TEMP "caddy-x") -Force
  Copy-Item (Join-Path $env:TEMP "caddy-x\caddy.exe") $caddy -Force
}
Ok (& $caddy version)
$winsw = Join-Path $Bin "WinSW-x64.exe"
if (-not (Test-Path $winsw)) {
  Invoke-WebRequest "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe" -OutFile $winsw
}
Ok "WinSW v2.12.0"

# ---------------------------------------------------------------- Caddyfile
$caddyfile = Join-Path $PSScriptRoot "Caddyfile"
$tpl = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "Caddyfile.template"))
Write-Utf8 $caddyfile ($tpl -replace "__DOMAIN__", $Domain -replace "__ROOT__", $RootFwd -replace "__PORT__", "$AppPort")
Quiet { & $caddy validate --config $caddyfile --adapter caddyfile }
if ($LASTEXITCODE -ne 0) { & $caddy validate --config $caddyfile --adapter caddyfile; Fail "Caddyfile не прошёл проверку" }
Ok "Caddyfile ok"

# ---------------------------------------------------------------- services
Step "Службы Windows"
function Install-Svc($id, $name, $desc, $exe, $arguments, $workdir) {
  $wrapper = Join-Path $Bin "$id.exe"
  $xml = Join-Path $Bin "$id.xml"
  if (Get-Service $id -ErrorAction SilentlyContinue) {
    Quiet { & $wrapper stop }
    Start-Sleep 2
    Quiet { & $wrapper uninstall }
    Start-Sleep 1
  }
  Copy-Item $winsw $wrapper -Force
  Write-Utf8 $xml @"
<service>
  <id>$id</id>
  <name>$name</name>
  <description>$desc</description>
  <executable>$exe</executable>
  <arguments>$arguments</arguments>
  <workingdirectory>$workdir</workingdirectory>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="5 sec"/>
  <onfailure action="restart" delay="15 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>20 sec</stoptimeout>
  <env name="PYTHONUTF8" value="1"/>
  <logpath>$Logs</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
</service>
"@
  & $wrapper install
  if ($LASTEXITCODE -ne 0) { Fail "Не удалось установить службу $id" }
}
Install-Svc "supplierhub-app" "SupplierHub" "SupplierHub: сайт, Telegram-бот, мониторинг платежей" `
  $VenvPy "`"$Root\run.py`"" $Root
Install-Svc "supplierhub-caddy" "SupplierHub HTTPS" "SupplierHub: Caddy (HTTPS, reverse proxy)" `
  $caddy "run --config `"$caddyfile`" --adapter caddyfile" $PSScriptRoot
Ok "установлены"

# ---------------------------------------------------------------- firewall + permissions
Step "Firewall и права на секреты"
if (-not (Get-NetFirewallRule -DisplayName "SupplierHub HTTP/HTTPS" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "SupplierHub HTTP/HTTPS" -Direction Inbound -Protocol TCP -LocalPort 80, 443 -Action Allow | Out-Null
}
# only Administrators and SYSTEM may read the private key and .env
icacls (Join-Path $Root "certs") /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
icacls $envFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
Ok "порты 80/443 открыты, certs и .env закрыты от других пользователей"

# ---------------------------------------------------------------- start
Step "Запуск"
& (Join-Path $Bin "supplierhub-app.exe") start
& (Join-Path $Bin "supplierhub-caddy.exe") start
Start-Sleep 8
try {
  $h = Invoke-RestMethod "http://127.0.0.1:$AppPort/healthz" -TimeoutSec 10
  Ok "приложение отвечает (nervixy_error: $($h.nervixy_error))"
} catch { Warn "приложение не отвечает — смотри $Logs\app.log и supplierhub-app.err.log" }
$code = & curl.exe -s -o NUL -w "%{http_code}" --resolve "${Domain}:443:127.0.0.1" "https://$Domain/healthz"
if ($code -eq "200") { Ok "HTTPS через Caddy работает" } else { Warn "HTTPS ответил кодом '$code' — смотри $Logs\supplierhub-caddy.err.log" }

$ip = try { (Invoke-RestMethod "https://api.ipify.org" -TimeoutSec 10) } catch { "?" }
Write-Host @"

Готово. Что осталось:
  1. DNS: A-записи $Domain и www.$Domain -> $ip
  2. Nervixy -> Настройки: добавь IP $ip в белый список API
  3. Nervixy -> Настройки -> Webhooks: URL https://$Domain/api/webhooks/nervixy ,
     секрет whsec_... впиши в .env (NERVIXY_WEBHOOK_SECRET) и выполни .\deploy\windows\update.ps1
  4. Открой https://$Domain

Управление:  Restart-Service supplierhub-app   |   логи: $Logs
"@ -ForegroundColor Cyan
