#Requires -RunAsAdministrator
<#
  SupplierHub — установка на Windows VDS (Windows Server 2019+/Windows 10+).

  1. Скопируй папку проекта на сервер, например в C:\supplierhub
  2. Положи сертификат: certs\fullchain.pem и certs\privkey.pem
  3. Заполни .env (образец — .env.example)
  4. PowerShell от администратора:
       cd C:\supplierhub
       powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1

  Ставит: Python + зависимости, nginx (HTTPS, прокси, лимиты), службы Windows с автозапуском,
  «fail2ban» для Windows (баны в брандмауэре за подбор пароля RDP и атаки на сайт).
  Скрипт можно запускать повторно — он переустановит всё с текущими настройками.
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
# and nginx/WinSW write to stderr even on success. Run them with a local "Continue"; judge them by $LASTEXITCODE.
function Quiet([scriptblock]$Cmd) { $ErrorActionPreference = "Continue"; & $Cmd 2>&1 | Out-Null }
function Loud([scriptblock]$Cmd) { $ErrorActionPreference = "Continue"; & $Cmd 2>&1 | ForEach-Object { "    $_" } }

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

# ---------------------------------------------------------------- backup password (generated here, shown once)
if ($envText -notmatch '(?m)^\s*BACKUP_PASSWORD\s*=\s*\S+') {
  Step "Пароль для бэкапов"
  $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_.!@%^*+'.ToCharArray()
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  $limit = 256 - (256 % $chars.Length)
  $sb = New-Object Text.StringBuilder
  $buf = New-Object byte[] 1
  while ($sb.Length -lt 32) { $rng.GetBytes($buf); if ($buf[0] -lt $limit) { [void]$sb.Append($chars[$buf[0] % $chars.Length]) } }
  $bp = $sb.ToString()
  if ($envText -match '(?m)^\s*BACKUP_PASSWORD\s*=') { $envText = $envText -replace '(?m)^\s*BACKUP_PASSWORD\s*=.*$', "BACKUP_PASSWORD=$bp" }
  else { $envText = $envText.TrimEnd() + "`r`n# daily DB backups are encrypted with this and sent to the admins in Telegram`r`nBACKUP_PASSWORD=$bp`r`n" }
  Write-Utf8 $envFile $envText
  Write-Host "`n    BACKUP_PASSWORD:  $bp`n" -ForegroundColor Yellow
  try { Set-Clipboard -Value $bp; Ok "(уже в буфере обмена)" } catch { }
  Warn "Сохрани его в менеджере паролей. Бот каждый день присылает зашифрованный бэкап базы, и без этого пароля"
  Warn "его не расшифровать. Пароль записан в .env, но если сервер умрёт, .env умрёт вместе с ним."
  Read-Host "    Нажми Enter, когда сохранишь" | Out-Null
}

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
Quiet { & $VenvPy -m pip install --quiet --upgrade pip }
& $VenvPy -m pip install --quiet -r (Join-Path $Root "requirements.txt") -c (Join-Path $Root "constraints.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install не прошёл" }
Ok "зависимости установлены"

# ---------------------------------------------------------------- old Caddy setup -> nginx
$caddySvc = Get-Service "supplierhub-caddy" -ErrorAction SilentlyContinue
if ($caddySvc) {
  Step "Убираю Caddy (теперь HTTPS держит nginx)"
  $w = Join-Path $Bin "supplierhub-caddy.exe"
  if (Test-Path $w) { Quiet { & $w stop }; Start-Sleep 2; Quiet { & $w uninstall }; Start-Sleep 1 }
  else { Quiet { sc.exe stop supplierhub-caddy }; Quiet { sc.exe delete supplierhub-caddy } }
  Remove-Item (Join-Path $Bin "supplierhub-caddy.*"), (Join-Path $PSScriptRoot "Caddyfile") -ErrorAction SilentlyContinue
  Ok "служба supplierhub-caddy удалена"
}

# ---------------------------------------------------------------- ports
Step "Порты 80/443"
$busy = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 80, 443 }
foreach ($b in $busy) {
  $proc = Get-Process -Id $b.OwningProcess -ErrorAction SilentlyContinue
  if ($proc.ProcessName -ne "nginx") {
    Warn "Порт $($b.LocalPort) занят процессом $($proc.ProcessName) (PID $($b.OwningProcess)). Если это IIS — останови: Stop-Service W3SVC; Set-Service W3SVC -StartupType Disabled"
  }
}
if (-not $busy) { Ok "свободны" }

# ---------------------------------------------------------------- nginx + WinSW
Step "nginx и WinSW"
$NginxDir = Join-Path $Bin "nginx"
$NginxExe = Join-Path $NginxDir "nginx.exe"
$NginxFwd = $NginxDir -replace '\\', '/'
if (-not (Test-Path $NginxExe)) {
  $ver = "1.30.5"
  try {
    $page = (Invoke-WebRequest "https://nginx.org/en/download.html" -UseBasicParsing).Content
    $m = [regex]::Match($page, 'Stable version.*?/download/nginx-(\d+\.\d+\.\d+)\.zip', 'Singleline')
    if ($m.Success) { $ver = $m.Groups[1].Value }
  } catch { Warn "не удалось узнать последнюю стабильную версию nginx, беру $ver" }
  $zip = Join-Path $env:TEMP "nginx-$ver.zip"
  Invoke-WebRequest "https://nginx.org/download/nginx-$ver.zip" -OutFile $zip
  $x = Join-Path $env:TEMP "nginx-x"
  Remove-Item $x -Recurse -Force -ErrorAction SilentlyContinue
  Expand-Archive $zip -DestinationPath $x -Force
  New-Item -ItemType Directory -Force $NginxDir | Out-Null
  Copy-Item (Join-Path $x "nginx-$ver\*") $NginxDir -Recurse -Force
}
New-Item -ItemType Directory -Force (Join-Path $NginxDir "logs"), (Join-Path $NginxDir "temp") | Out-Null
Ok ((Loud { & $NginxExe -v }) -join " ").Trim()
$winsw = Join-Path $Bin "WinSW-x64.exe"
if (-not (Test-Path $winsw)) {
  Invoke-WebRequest "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe" -OutFile $winsw
}
Ok "WinSW v2.12.0"

$tpl = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "nginx.conf.template"))
Write-Utf8 (Join-Path $NginxDir "conf\nginx.conf") ($tpl -replace "__DOMAIN__", $Domain -replace "__ROOT__", $RootFwd -replace "__PORT__", "$AppPort")
Quiet { & $NginxExe -p "$NginxFwd/" -t }
if ($LASTEXITCODE -ne 0) { Loud { & $NginxExe -p "$NginxFwd/" -t }; Fail "nginx.conf не прошёл проверку" }
Ok "nginx.conf ok"

# ---------------------------------------------------------------- services
Step "Службы Windows"
function Install-Svc($id, $name, $desc, $exe, $arguments, $workdir, $stopArguments = "") {
  $wrapper = Join-Path $Bin "$id.exe"
  $xml = Join-Path $Bin "$id.xml"
  if (Get-Service $id -ErrorAction SilentlyContinue) {
    Quiet { & $wrapper stop }
    Start-Sleep 2
    Quiet { & $wrapper uninstall }
    Start-Sleep 1
  }
  Copy-Item $winsw $wrapper -Force
  $stop = if ($stopArguments) { "<stopexecutable>$exe</stopexecutable>`n  <stoparguments>$stopArguments</stoparguments>" } else { "" }
  Write-Utf8 $xml @"
<service>
  <id>$id</id>
  <name>$name</name>
  <description>$desc</description>
  <executable>$exe</executable>
  <arguments>$arguments</arguments>
  $stop
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
  Quiet { & $wrapper install }
  if ($LASTEXITCODE -ne 0) { Fail "Не удалось установить службу $id" }
}
Install-Svc "supplierhub-app" "SupplierHub" "SupplierHub: сайт, Telegram-бот, мониторинг платежей" `
  $VenvPy "`"$Root\run.py`"" $Root
Install-Svc "supplierhub-nginx" "SupplierHub HTTPS" "SupplierHub: nginx (HTTPS, прокси, лимиты)" `
  $NginxExe "-p `"$NginxFwd/`"" $NginxDir "-p `"$NginxFwd/`" -s quit"
Ok "установлены"

# ---------------------------------------------------------------- firewall + permissions
Step "Firewall и права на секреты и данные"
if (-not (Get-NetFirewallRule -DisplayName "SupplierHub HTTP/HTTPS" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "SupplierHub HTTP/HTTPS" -Direction Inbound -Protocol TCP -LocalPort 80, 443 -Action Allow | Out-Null
}
# only Administrators and SYSTEM (the services run as SYSTEM) may read the private key, .env and the database
icacls (Join-Path $Root "certs") /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
icacls $envFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
icacls (Join-Path $Root "data") /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
Ok "порты 80/443 открыты; certs, .env и data (база, бэкапы, логи) закрыты от других пользователей"

# ---------------------------------------------------------------- fail2ban for Windows
Step "Защита от перебора (fail2ban для Windows)"
# failed logons (event 4625) must be audited; the GUID is the "Logon" subcategory in any Windows language
Quiet { auditpol /set /subcategory:"{0CCE9215-69AE-11D9-BED3-505054503030}" /failure:enable }
$rdpPort = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" -ErrorAction SilentlyContinue).PortNumber
if (-not $rdpPort) { $rdpPort = 3389 }
$mine = @(Get-NetTCPConnection -LocalPort $rdpPort -State Established -ErrorAction SilentlyContinue |
          ForEach-Object { $_.RemoteAddress } | Where-Object { $_ -notmatch '^(127\.|::1$|10\.|192\.168\.)' } | Sort-Object -Unique)
$envText = Get-Content $envFile -Raw
$wlLine = [regex]::Match($envText, '(?m)^\s*F2B_WHITELIST\s*=(.*)$')
$wl = if ($wlLine.Success) { $wlLine.Groups[1].Value.Trim() } else { "" }
$add = @($mine | Where-Object { $wl -notmatch [regex]::Escape($_) })
if ($add.Count) {
  $newWl = (@($wl) + $add | Where-Object { $_ }) -join ","
  if ($wlLine.Success) { $envText = $envText -replace '(?m)^\s*F2B_WHITELIST\s*=.*$', "F2B_WHITELIST=$newWl" }
  else { $envText = $envText.TrimEnd() + "`r`n# never banned by the site or the RDP guard (your own IPs)`r`nF2B_WHITELIST=$newWl`r`n" }
  Write-Utf8 $envFile $envText
  Ok "твой текущий IP ($($add -join ', ')) добавлен в F2B_WHITELIST — его не забанят ни сайт, ни RDP-страж"
} elseif ($wl) { Ok "белый список: $wl" }
$guard = Join-Path $PSScriptRoot "guard.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$guard`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$tset = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
  -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "SupplierHub Guard" -Action $action -Trigger $trigger -Principal $principal `
  -Settings $tset -Force | Out-Null
Ok "задача «SupplierHub Guard» раз в минуту: 8 неудачных входов по RDP за 30 мин → бан IP (1 ч → 24 ч → 7 дней);"
Ok "IP, забаненные сайтом (сканеры, подбор API-ключей), тоже закрываются в брандмауэре. Лог: data\logs\guard.log"

# ---------------------------------------------------------------- start
Step "Запуск"
Quiet { & (Join-Path $Bin "supplierhub-app.exe") start }
Quiet { & (Join-Path $Bin "supplierhub-nginx.exe") start }
Start-Sleep 8
try {
  $h = Invoke-RestMethod "http://127.0.0.1:$AppPort/healthz" -TimeoutSec 10
  Ok "приложение отвечает (nervixy_error: $($h.nervixy_error))"
} catch { Warn "приложение не отвечает — смотри $Logs\app.log и supplierhub-app.err.log" }
$code = & curl.exe -s -o NUL -w "%{http_code}" --resolve "${Domain}:443:127.0.0.1" "https://$Domain/healthz"
if ($code -eq "200") { Ok "HTTPS через nginx работает" } else { Warn "HTTPS ответил кодом '$code' — смотри $NginxDir\logs\error.log" }

$ip = try { (Invoke-RestMethod "https://api.ipify.org" -TimeoutSec 10) } catch { "?" }
Write-Host @"

Готово. Что осталось:
  1. DNS: A-записи $Domain и www.$Domain -> $ip
  2. Nervixy -> Настройки: добавь IP $ip в белый список API
  3. Nervixy -> Настройки -> Webhooks: URL https://$Domain/api/webhooks/nervixy ,
     секрет whsec_... впиши в .env (NERVIXY_WEBHOOK_SECRET) и выполни .\deploy\windows\update.ps1
  4. Смени пароль Windows на случайный из 24 символов:
     powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-password.ps1
  5. Открой https://$Domain

Управление:  Restart-Service supplierhub-app   |   логи: $Logs
Баны:        Get-Content data\logs\guard.log -Tail 20   |   снять все: Remove-NetFirewallRule -DisplayName "SupplierHub ban"
"@ -ForegroundColor Cyan
