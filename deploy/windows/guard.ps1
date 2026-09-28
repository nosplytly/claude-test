<#
  SupplierHub guard: fail2ban for Windows. install.ps1 runs it every minute as SYSTEM (task "SupplierHub Guard").

  1. RDP / Windows logon brute force: failed logons (Security 4625, RdpCoreTS 140) are counted per source IP;
     MaxFails within WindowMin minutes -> that IP is blocked in Windows Firewall. Repeat offenders within a week
     get longer bans: 1 h -> 24 h -> 7 days.
  2. IPs banned by the site itself (data\f2b-bans.txt: vulnerability scanners, API-key guessing, ...) are blocked
     in the firewall as well, so they can't even open a connection.
  3. nginx logs above 20 MB are rotated.

  Never banned: loopback, private ranges and F2B_WHITELIST from .env (put your home/office IP there).
  Every ban lives in ONE firewall rule, "SupplierHub ban" - deleting that rule lifts all bans at once:
      Remove-NetFirewallRule -DisplayName "SupplierHub ban"
  Dry run (prints what it would do, changes nothing):
      powershell -ExecutionPolicy Bypass -File .\deploy\windows\guard.ps1 -DryRun
#>
param(
  [int]$MaxFails = 8,
  [int]$WindowMin = 30,
  [double]$BanHours = 1,
  [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Data = Join-Path $Root "data"
$StateFile = Join-Path $Data "guard-state.json"
$AppBans = Join-Path $Data "f2b-bans.txt"
$LogDir = Join-Path $Data "logs"
$LogFile = Join-Path $LogDir "guard.log"
$RuleName = "SupplierHub ban"
$NginxDir = Join-Path $PSScriptRoot "bin\nginx"
$MaxRuleAddresses = 1000
$Now = [DateTime]::UtcNow

function Log([string]$msg) {
  if ($DryRun) { Write-Host $msg; return }
  New-Item -ItemType Directory -Force $LogDir | Out-Null
  if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) { Move-Item $LogFile "$LogFile.1" -Force }
  Add-Content -Path $LogFile -Value ("{0:yyyy-MM-dd HH:mm:ss}Z {1}" -f $Now, $msg) -Encoding UTF8
}

# ------------------------------------------------------------------ addresses
function Parse-Net([string]$text) {
  # "1.2.3.4", "1.2.0.0/16", "2001:db8::/64" -> @{Bytes; Prefix} or $null
  $parts = $text.Trim().Split("/")
  $ip = $null
  if (-not [System.Net.IPAddress]::TryParse($parts[0], [ref]$ip)) { return $null }
  if ($ip.IsIPv4MappedToIPv6) { $ip = $ip.MapToIPv4() }
  $bytes = $ip.GetAddressBytes()
  $prefix = $bytes.Length * 8
  if ($parts.Length -gt 1) {
    $p = 0
    if (-not [int]::TryParse($parts[1], [ref]$p) -or $p -lt 0 -or $p -gt $prefix) { return $null }
    $prefix = $p
  }
  return @{ Bytes = $bytes; Prefix = $prefix }
}

function In-Net($ipBytes, $net) {
  if ($ipBytes.Length -ne $net.Bytes.Length) { return $false }
  $bits = $net.Prefix
  for ($i = 0; $i -lt $ipBytes.Length -and $bits -gt 0; $i++) {
    $take = [Math]::Min(8, $bits)
    $mask = [byte]((0xFF -shl (8 - $take)) -band 0xFF)
    if (($ipBytes[$i] -band $mask) -ne ($net.Bytes[$i] -band $mask)) { return $false }
    $bits -= $take
  }
  return $true
}

$Never = @("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
           "::1/128", "fe80::/10", "fc00::/7") | ForEach-Object { Parse-Net $_ }
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
  $line = Get-Content $envFile | Where-Object { $_ -match '^\s*F2B_WHITELIST\s*=' } | Select-Object -Last 1
  if ($line) {
    foreach ($w in ($line -replace '^\s*F2B_WHITELIST\s*=\s*', '') -split '[,\s]+') {
      if ($w) { $n = Parse-Net $w; if ($n) { $Never += $n } else { Log "F2B_WHITELIST: '$w' is not an IP/subnet, skipped" } }
    }
  }
}

function Is-Bannable([string]$addr) {
  # a single address or a /64 from the app; $false for garbage, loopback, private and whitelisted
  $n = Parse-Net $addr
  if (-not $n) { return $false }
  foreach ($w in $Never) { if (In-Net $n.Bytes $w) { return $false } }
  return $true
}

# ------------------------------------------------------------------ state
$state = @{}
if (Test-Path $StateFile) {
  try {
    $raw = Get-Content $StateFile -Raw | ConvertFrom-Json
    foreach ($p in $raw.PSObject.Properties) {
      $state[$p.Name] = @{ until = [DateTime]::Parse($p.Value.until).ToUniversalTime(); n = [int]$p.Value.n;
                           last = [DateTime]::Parse($p.Value.last).ToUniversalTime(); reason = [string]$p.Value.reason }
    }
  } catch { Log "state file unreadable, starting clean: $($_.Exception.Message)" }
}
foreach ($k in @($state.Keys)) {  # forget offenders quiet for a week
  if ($state[$k].until -lt $Now -and ($Now - $state[$k].last).TotalDays -gt 7) { $state.Remove($k) }
}

# ------------------------------------------------------------------ 1. failed logons
$fails = New-Object 'System.Collections.Generic.Dictionary[string,int]'
function Count-Fails([hashtable]$filter, [string]$field) {
  try {
    $events = Get-WinEvent -FilterHashtable $filter -MaxEvents 20000 -ErrorAction Stop
  } catch {
    if ($_.Exception.Message -notmatch "No events were found|could not be found|not find") {
      Log "reading $($filter.LogName): $($_.Exception.Message)"
    }
    return
  }
  foreach ($ev in $events) {
    $x = [xml]$ev.ToXml()
    $ip = ($x.Event.EventData.Data | Where-Object { $_.Name -eq $field } | Select-Object -First 1).'#text'
    if ($ip -and $ip -ne "-" -and (Is-Bannable $ip)) {
      $ip = (New-Object System.Net.IPAddress (, (Parse-Net $ip).Bytes)).ToString()  # ::ffff:1.2.3.4 -> 1.2.3.4
      if ($fails.ContainsKey($ip)) { $fails[$ip]++ } else { $fails[$ip] = 1 }
    }
  }
}
$since = (Get-Date).AddMinutes(-$WindowMin)
Count-Fails @{ LogName = "Security"; Id = 4625; StartTime = $since } "IpAddress"
Count-Fails @{ LogName = "Microsoft-Windows-RemoteDesktopServices-RdpCoreTS/Operational"; Id = 140; StartTime = $since } "IPString"

foreach ($ip in $fails.Keys) {
  if ($fails[$ip] -lt $MaxFails) { continue }
  $old = $state[$ip]
  if ($old -and $old.until -gt $Now) { continue }  # already banned
  $n = if ($old -and ($Now - $old.last).TotalDays -le 7) { $old.n } else { 0 }
  $hours = [Math]::Min($BanHours * [Math]::Pow(24, $n), 168)
  $state[$ip] = @{ until = $Now.AddHours($hours); n = $n + 1; last = $Now; reason = "$($fails[$ip]) failed logons" }
  Log "BAN $ip for $hours h: $($fails[$ip]) failed logons in $WindowMin min (ban #$($n + 1))"
}

# ------------------------------------------------------------------ 2. firewall rule = RDP bans + the site's bans
$desired = New-Object 'System.Collections.Generic.List[string]'
foreach ($k in ($state.Keys | Sort-Object { $state[$_].until } -Descending)) {
  if ($state[$k].until -gt $Now) { $desired.Add($k) }
}
if (Test-Path $AppBans) {
  foreach ($a in Get-Content $AppBans) {
    $a = $a.Trim()
    if ($a -and (Is-Bannable $a) -and -not $desired.Contains($a)) { $desired.Add($a) }
  }
}
if ($desired.Count -gt $MaxRuleAddresses) { $desired = $desired.GetRange(0, $MaxRuleAddresses) }

$rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($desired.Count -eq 0) {
  # never leave a block rule with an empty list: an empty RemoteAddress means "Any" and would lock everyone out
  if ($rule) {
    if ($DryRun) { Write-Host "would remove rule '$RuleName' (no active bans)" }
    else { Remove-NetFirewallRule -DisplayName $RuleName; Log "no active bans: rule removed" }
  }
} else {
  $current = @()
  if ($rule) { $current = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress) }
  $same = ($current.Count -eq $desired.Count) -and -not (Compare-Object $current $desired.ToArray())
  if (-not $same) {
    if ($DryRun) {
      Write-Host "would block $($desired.Count) address(es): $($desired -join ', ')"
    } elseif ($rule) {
      Set-NetFirewallRule -DisplayName $RuleName -RemoteAddress $desired.ToArray()
      Log "rule updated: $($desired.Count) blocked"
    } else {
      New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Block -Profile Any `
        -RemoteAddress $desired.ToArray() -Description "SupplierHub guard (deploy\windows\guard.ps1)" | Out-Null
      Log "rule created: $($desired.Count) blocked"
    }
  }
}

# ------------------------------------------------------------------ 3. nginx log rotation
$rotated = $false
foreach ($f in @("access.log", "error.log")) {
  $p = Join-Path $NginxDir "logs\$f"
  if ((Test-Path $p) -and (Get-Item $p).Length -gt 20MB) {
    if ($DryRun) { Write-Host "would rotate $p" } else { Move-Item $p "$p.1" -Force; $rotated = $true }
  }
}
if ($rotated) {
  $ErrorActionPreference = "Continue"
  & (Join-Path $NginxDir "nginx.exe") -p "$($NginxDir -replace '\\', '/')/" -s reopen 2>&1 | Out-Null
  Log "nginx logs rotated"
}

# ------------------------------------------------------------------ save
if (-not $DryRun) {
  $out = @{}
  foreach ($k in $state.Keys) {
    $out[$k] = @{ until = $state[$k].until.ToString("o"); n = $state[$k].n; last = $state[$k].last.ToString("o");
                  reason = $state[$k].reason }
  }
  $tmp = "$StateFile.tmp"
  ($out | ConvertTo-Json -Depth 3) | Set-Content -Path $tmp -Encoding UTF8
  Move-Item $tmp $StateFile -Force
} else {
  Write-Host "failed logons in the last $WindowMin min: $(if ($fails.Count) { ($fails.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ', ' } else { 'none' })"
  Write-Host "site bans file: $(if (Test-Path $AppBans) { (Get-Content $AppBans).Count } else { 'none yet' })"
}
