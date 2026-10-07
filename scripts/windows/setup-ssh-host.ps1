# Turns this Windows machine into an SSH-reachable test box for the desktop app.
# Installs and starts OpenSSH Server, opens port 22, makes PowerShell the SSH shell and authorizes the given keys.
# Bootstrap without touching the dev machine's key on this machine:
#   1. here, in an elevated PowerShell (the one step that needs someone at this machine for the UAC prompt):
#        powershell -ExecutionPolicy Bypass -File scripts\windows\setup-ssh-host.ps1 -NoSleepOnAC
#      sshd now accepts this account's Windows password
#   2. from the dev machine: scripts/win-remote.sh authorize
#      logs in with the password once, authorizes the dev machine's key and turns password login off.
#      An SSH login of an Administrators member is already elevated, so this rerun needs no UAC prompt.
#Requires -RunAsAdministrator
param(
  [string[]]$PublicKey = @(),     # key lines, e.g. "ssh-ed25519 AAAA... me@mac"
  [string[]]$PublicKeyFile = @(), # .pub files holding key lines
  [switch]$NoSleepOnAC,           # keep the machine awake while plugged in, so it stays reachable
  [switch]$DisablePasswordAuth,   # key-only login from now on (refused while no key is authorized)
  [switch]$FromStartupTask        # internal: run by the startup task registered below
)

$startupTask = @{ TaskName = 'mentora-ssh-setup'; TaskPath = '\mentora\' }
if ($FromStartupTask) {
  Start-Transcript -Path "$env:ProgramData\ssh\mentora-setup.log" -Append | Out-Null
  # servicing may still be registering the service right after boot
  $deadline = (Get-Date).AddMinutes(5)
  while (-not (Get-Service sshd -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 10 }
}

$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
if ($cap.State -ne 'Installed' -and -not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  Write-Host "Installing $($cap.Name) ..."
  Add-WindowsCapability -Online -Name $cap.Name -ErrorAction Stop | Out-Null
}
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  if ($FromStartupTask) { throw 'sshd is still missing after the restart; run this script again as administrator.' }
  # Some builds only finish installing the capability during the next restart. Finish this setup at startup
  # as SYSTEM, so nobody has to log on and approve a UAC prompt again.
  $taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -FromStartupTask"
  if ($NoSleepOnAC) { $taskArgs += ' -NoSleepOnAC' }
  foreach ($k in $PublicKey) { $taskArgs += " -PublicKey `"$k`"" }
  foreach ($f in $PublicKeyFile) { $taskArgs += " -PublicKeyFile `"$((Resolve-Path $f).Path)`"" }
  Register-ScheduledTask @startupTask -Force -ErrorAction Stop `
    -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs) `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) -User 'SYSTEM' -RunLevel Highest | Out-Null
  Write-Warning 'OpenSSH Server finishes installing on the next restart. Restart Windows: the rest of this setup runs by itself at startup (log: C:\ProgramData\ssh\mentora-setup.log).'
  return
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
Write-Host "sshd: $((Get-Service sshd).Status)"

# The capability normally creates this rule; recreate it if it is missing or disabled
$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if (-not $rule) {
  New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True `
    -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
} else {
  Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any
}

# Remote commands run in Windows PowerShell instead of cmd.exe
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
  -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null

# Members of Administrators are authorized from this shared file, not from ~/.ssh/authorized_keys
$keys = @($PublicKey) + @($PublicKeyFile | ForEach-Object { Get-Content $_ -ErrorAction Stop })
$keys = @($keys | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^(ssh-|ecdsa-|sk-)' })
if ($keys.Count -gt 0) {
  $file = "$env:ProgramData\ssh\administrators_authorized_keys"
  $existing = @(if (Test-Path $file) { Get-Content $file })
  $new = @($keys | Where-Object { $existing -notcontains $_ })
  if ($new.Count -gt 0) { Add-Content -Path $file -Value $new -Encoding ascii }
  # sshd ignores the file unless only Administrators and SYSTEM can access it
  icacls.exe $file /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
  Write-Host "Authorized keys: $($new.Count) added, $($existing.Count) already present ($file)"
} else {
  Write-Host 'No public key given: password login (this account''s Windows password) stays on.'
}

if ($NoSleepOnAC) {
  powercfg /change standby-timeout-ac 0
  Write-Host 'Sleep on AC power: disabled'
}

$ips = Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
  ForEach-Object { $_.IPAddress }
Write-Host ''
Write-Host 'Done. From the dev machine:'
foreach ($ip in $ips) { Write-Host "  ssh $env:USERNAME@$ip" }

if ($FromStartupTask) {
  Unregister-ScheduledTask @startupTask -Confirm:$false
  Stop-Transcript | Out-Null
}

if ($DisablePasswordAuth) {
  $keyFile = "$env:ProgramData\ssh\administrators_authorized_keys"
  if (-not ((Test-Path $keyFile) -and (Get-Content $keyFile | Where-Object { $_ -match '^(ssh-|ecdsa-|sk-)' }))) {
    throw "No authorized key in $keyFile; password login stays on so nobody is locked out."
  }
  # Set it before the first Match block: the stock config ends with "Match Group administrators",
  # and anything after that line would only apply inside the match
  $config = "$env:ProgramData\ssh\sshd_config"
  $lines = @(Get-Content $config | Where-Object { $_ -notmatch '^\s*#?\s*PasswordAuthentication\b' })
  Set-Content -Path $config -Value (@('PasswordAuthentication no') + $lines) -Encoding ascii
  Write-Host 'Password login: off (key only). Restarting sshd; an SSH session running this may drop, reconnect with the key.'
  # last step on purpose: the key is already in place if the restart cuts this session
  Restart-Service sshd
}
