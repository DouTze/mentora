# Helpers shared by the scripts that the dev machine runs over SSH (deploy.ps1, screenshot.ps1).
# A process started from an SSH session lives in a non-interactive session and never shows on screen,
# so anything that must appear on the desktop is started through a scheduled task in the logged-on user's session.
# An administrator's SSH session is also elevated, and elevated processes do not follow junctions that a normal user
# created, such as scoop's apps\<app>\current or uv's Python links: git, bun, uv and cargo are "not found" there.
# deploy.ps1 therefore does its work in the desktop session too (Invoke-OnDesktop).

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
  param([Parameter(Mandatory)][string]$Exe, [string]$Arguments = '', [string]$WorkingDirectory = (Split-Path $Exe),
    [string]$Name = [IO.Path]::GetFileNameWithoutExtension($Exe)) # tasks with different names can run side by side
  if (-not (Test-DesktopSession)) {
    throw "$env:USERNAME is not logged on to the desktop of this machine; log on (a locked screen is fine) and retry."
  }
  $name = "mentora-remote-$Name"
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
    -Name ([IO.Path]::GetFileNameWithoutExtension($Script)) `
    -Arguments "--headless `"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$Script`" $Arguments"
}

function Test-Elevated {
  ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-OnDesktop {
  # Runs a PowerShell script in the user's desktop session with a normal (non-elevated) token, streams its output
  # here while it runs and returns its exit code. $Arguments must not contain double quotes.
  param([Parameter(Mandatory)][string]$Script, [string]$Arguments = '')
  $base = "$env:TEMP\mentora-remote-$([IO.Path]::GetFileNameWithoutExtension($Script))"
  $log = "$base.log"; $exitFile = "$base.exit"; $bat = "$base.cmd"
  Remove-Item $log, $exitFile -ErrorAction SilentlyContinue
  # a batch file sidesteps nested quoting, and %errorlevel% is expanded per line there
  Set-Content -Path $bat -Encoding ascii -Value @(
    '@echo off'
    "`"$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$Script`" $Arguments > `"$log`" 2>&1"
    "> `"$exitFile`" echo %errorlevel%"
  )
  Start-OnDesktop -Exe "$env:SystemRoot\System32\conhost.exe" -Arguments "--headless cmd.exe /c `"$bat`"" `
    -WorkingDirectory (Split-Path $Script) -Name ([IO.Path]::GetFileNameWithoutExtension($Script))

  $pos = 0
  $decoder = [Text.Encoding]::UTF8.GetDecoder() # keeps characters split across two reads intact
  $bytes = New-Object byte[] 65536
  $chars = New-Object char[] 65536
  while ($true) {
    $done = Test-Path $exitFile # checked before reading, so the last read sees everything written before the exit
    if (Test-Path $log) {
      $fs = [IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
      [void]$fs.Seek($pos, 'Begin')
      while (($n = $fs.Read($bytes, 0, $bytes.Length)) -gt 0) {
        $pos += $n
        $c = $decoder.GetChars($bytes, 0, $n, $chars, 0)
        [Console]::Out.Write($chars, 0, $c)
      }
      $fs.Close()
    }
    if ($done) { break }
    Start-Sleep -Milliseconds 500
  }
  [Console]::Out.Flush()
  [int](Get-Content $exitFile -Raw).Trim()
}
