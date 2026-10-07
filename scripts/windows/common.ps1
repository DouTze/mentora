# Helpers shared by the scripts that the dev machine runs over SSH (deploy.ps1, screenshot.ps1).
# A process started from an SSH session lives in a non-interactive session and never shows on screen,
# so anything that must appear on the desktop is started through a scheduled task in the logged-on user's session.

[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Invoke-Native {
  # Runs an external command and stops the script when it fails (native exit codes do not throw on their own).
  # No param block on purpose: flags such as --no-bundle must reach the command instead of PowerShell's binder.
  # @(...) matters: with a single argument $rest would be a plain string, and splatting a string passes its characters
  $exe = $args[0]
  $rest = @($args | Select-Object -Skip 1)
  & $exe @rest
  if ($LASTEXITCODE -ne 0) { throw "$exe $($rest -join ' ') exited with $LASTEXITCODE" }
}

function Test-DesktopSession {
  # True when this user has a desktop session that is logged on (explorer.exe running outside session 0)
  $me = "$env:USERDOMAIN\$env:USERNAME"
  foreach ($p in Get-CimInstance Win32_Process -Filter "Name='explorer.exe'") {
    if ($p.SessionId -eq 0) { continue }
    $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner
    if ("$($o.Domain)\$($o.User)" -eq $me) { return $true }
  }
  return $false
}

function Start-OnDesktop {
  # Starts $Exe on the logged-on user's desktop and returns right away
  param([Parameter(Mandatory)][string]$Exe, [string]$Arguments = '', [string]$WorkingDirectory = (Split-Path $Exe))
  if (-not (Test-DesktopSession)) {
    throw "$env:USERNAME is not logged on to the desktop of this machine; log on (a locked screen is fine) and retry."
  }
  $name = 'mentora-remote-' + [IO.Path]::GetFileNameWithoutExtension($Exe)
  $action = if ($Arguments) {
    New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $WorkingDirectory
  } else {
    New-ScheduledTaskAction -Execute $Exe -WorkingDirectory $WorkingDirectory
  }
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
  Register-ScheduledTask -TaskName $name -TaskPath '\mentora\' -Action $action -Principal $principal `
    -Settings $settings -Force -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $name -TaskPath '\mentora\' -ErrorAction Stop
}

function Start-HiddenScriptOnDesktop {
  # Runs a PowerShell script in the user's desktop session without opening a console window
  param([Parameter(Mandatory)][string]$Script, [string]$Arguments = '')
  $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  Start-OnDesktop -Exe "$env:SystemRoot\System32\conhost.exe" -WorkingDirectory (Split-Path $Script) `
    -Arguments "--headless `"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$Script`" $Arguments"
}
