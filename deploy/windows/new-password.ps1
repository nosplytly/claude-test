#Requires -RunAsAdministrator
<#
  Надёжный пароль: 16–32 символа (по умолчанию 24) — заглавные, строчные, цифры и спецсимволы.
  Генерируется прямо на сервере криптографическим генератором; кроме тебя его никто не видит.

  Сменить пароль текущему пользователю (тому, под которым ты зашёл по RDP):
      powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-password.ps1
  Другому пользователю / другой длины:
      powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-password.ps1 -User Administrator -Length 32
  Только показать новый пароль (для панели хостинга, Nervixy, почты...), ничего не меняя:
      powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-password.ps1 -Only
#>
param(
  [string]$User = $env:USERNAME,
  [ValidateRange(16, 32)][int]$Length = 24,
  [switch]$Only
)
$ErrorActionPreference = "Stop"

# без похожих друг на друга символов (0/O, 1/l/I) и без тех, что ломаются в командной строке и формах (' " ` \ < > | пробел)
$Groups = @("ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%^&*-_=+?.:~")
$rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider

function Rand([int]$n) {
  # равномерно в [0, n): отбрасываем «хвост», чтобы не было перекоса по модулю
  $buf = New-Object byte[] 4
  $limit = [uint32]::MaxValue - ([uint32]::MaxValue % [uint32]$n)
  do { $rng.GetBytes($buf); $v = [BitConverter]::ToUInt32($buf, 0) } while ($v -ge $limit)
  return [int]($v % [uint32]$n)
}

function New-StrongPassword([int]$len) {
  $chars = New-Object System.Collections.Generic.List[char]
  foreach ($g in $Groups) { 1..2 | ForEach-Object { $chars.Add($g[(Rand $g.Length)]) } }  # по 2 из каждой группы
  $all = -join $Groups
  while ($chars.Count -lt $len) { $chars.Add($all[(Rand $all.Length)]) }
  for ($i = $chars.Count - 1; $i -gt 0; $i--) {  # перемешать (Фишер–Йетс)
    $j = Rand ($i + 1)
    $t = $chars[$i]; $chars[$i] = $chars[$j]; $chars[$j] = $t
  }
  return -join $chars
}

$pw = New-StrongPassword $Length

if ($Only) {
  Write-Host "`n  $pw`n" -ForegroundColor Green
  try { Set-Clipboard -Value $pw; Write-Host "  (уже в буфере обмена)" -ForegroundColor DarkGray } catch { }
  Write-Host "  Сохрани его в менеджере паролей." -ForegroundColor Yellow
  exit 0
}

if (-not (Get-LocalUser -Name $User -ErrorAction SilentlyContinue)) {
  Write-Host "Локальный пользователь '$User' не найден. Список: $((Get-LocalUser | Where-Object Enabled).Name -join ', ')" -ForegroundColor Red
  exit 1
}
Write-Host "Сменить пароль Windows пользователю '$User' на новый случайный ($Length символов)?" -ForegroundColor Cyan
Write-Host "Текущий RDP-сеанс не прервётся; новый пароль нужен при следующем входе." -ForegroundColor Cyan
$answer = Read-Host "Введи 'да' для подтверждения"
if ($answer -notin @("да", "Да", "ДА", "y", "yes")) { Write-Host "Отменено, пароль не менялся."; exit 0 }

Set-LocalUser -Name $User -Password (ConvertTo-SecureString $pw -AsPlainText -Force)
Write-Host "`nНовый пароль пользователя ${User}:`n" -ForegroundColor Green
Write-Host "  $pw`n" -ForegroundColor Green
try { Set-Clipboard -Value $pw; Write-Host "  (уже в буфере обмена — вставь в менеджер паролей)" -ForegroundColor DarkGray } catch { }
Write-Host @"

  1. Сохрани пароль в менеджере паролей ПРЯМО СЕЙЧАС — восстановить его нельзя.
  2. Не закрывая это окно, открой второе RDP-подключение и проверь вход с новым паролем.
  3. Если что-то пошло не так — пароль сбрасывается через консоль (VNC) в панели хостинга.
"@ -ForegroundColor Yellow
