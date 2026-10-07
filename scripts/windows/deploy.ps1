# Build the desktop app on this Windows machine and open it on its desktop. Meant to be run over SSH from the
# dev machine (scripts/win-remote.sh deploy), but works the same from a local terminal.
#   deploy.ps1 -Ref <commit> -Fetch  fetch origin, check out that commit (detached), build, run target\release\mentora.exe
#   deploy.ps1 -Ref origin/develop -Fetch
#   deploy.ps1                       build and run the current checkout
#   deploy.ps1 -Install              build the NSIS installer, install it silently and run the installed app
#   deploy.ps1 -NoBuild              just restart the last build
param(
  [string]$Ref = '',  # branch, tag or commit to check out (detached); '' keeps the current checkout
  [switch]$Fetch,     # git fetch origin first; commits only reach this machine through origin
  [switch]$Install,
  [switch]$NoBuild,
  [switch]$NoLaunch
)
. "$PSScriptRoot\common.ps1"
# no global ErrorActionPreference=Stop: in Windows PowerShell 5.1 it turns progress that git/bun print on stderr into errors

if (Test-Elevated) {
  # Over SSH this runs elevated, where scoop/uv tools cannot be found (see common.ps1): redo it on the desktop,
  # unelevated, which also gives git the user's GitHub credentials
  $fwd = @()
  if ($Ref) { $fwd += "-Ref $Ref" }
  foreach ($s in 'Fetch', 'Install', 'NoBuild', 'NoLaunch') { if ((Get-Variable $s -ValueOnly).IsPresent) { $fwd += "-$s" } }
  exit (Invoke-OnDesktop -Script $PSCommandPath -Arguments ($fwd -join ' '))
}

$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $root

function Stop-App {
  # The running app locks its exe and the sidecar, which would break the build or the install
  $procs = @(Get-Process -Name mentora, mentor-backend -ErrorAction SilentlyContinue)
  if ($procs.Count -gt 0) {
    Write-Host "Stopping $($procs.Count) running Mentora process(es)"
    $procs | Stop-Process -Force
    Start-Sleep -Seconds 1
  }
}

function Find-InstalledExe {
  $dir = "$env:LOCALAPPDATA\Mentora" # Tauri's NSIS installer installs per user here by default
  Get-ChildItem $dir -Filter *.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch '^(uninstall|mentor-backend)' } |
    Select-Object -First 1 -ExpandProperty FullName
}

if ($Ref) {
  if ($Fetch) { Invoke-Native git fetch origin --prune }
  if (git status --porcelain --untracked-files=no) {
    throw "Uncommitted changes in $root; commit or stash them on this machine first."
  }
  git rev-parse --verify --quiet "$Ref^{commit}" | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "$Ref is not in this repo; push it to origin first (and pass -Fetch)." }
  Invoke-Native git checkout --quiet --detach $Ref
}
Write-Host "Commit: $(git log -1 --format='%h %s (%cr)')"

Stop-App

if (-not $NoBuild) {
  foreach ($tool in 'bun', 'uv', 'cargo') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not on PATH for this user." }
  }
  Invoke-Native bun install
  # beforeBuildCommand (bun run desktop:prepare) builds the frontend and the Python sidecar first
  if ($Install) {
    Invoke-Native bun run tauri build --bundles nsis
  } else {
    Invoke-Native bun run tauri build --no-bundle
  }
}

if ($Install) {
  $setup = Get-ChildItem "$root\src-tauri\target\release\bundle\nsis\*-setup.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $setup) { throw 'No NSIS installer found; run without -NoBuild first.' }
  Write-Host "Installing $($setup.Name) silently"
  $p = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "Installer exited with $($p.ExitCode)" }
  $exe = Find-InstalledExe
  if (-not $exe) { throw "Installed app not found under $env:LOCALAPPDATA\Mentora" }
} else {
  $exe = "$root\src-tauri\target\release\mentora.exe"
  if (-not (Test-Path $exe)) { throw "$exe not found; run without -NoBuild first." }
}

if ($NoLaunch) {
  Write-Host "Built: $exe"
  return
}

Start-OnDesktop -Exe $exe
Start-Sleep -Seconds 5
$running = @(Get-Process -Name mentora, mentor-backend -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessName })
Write-Host "Launched $exe"
Write-Host "Running: $(if ($running) { $running -join ', ' } else { 'nothing (check the log below)' })"
Write-Host "Backend log: $env:LOCALAPPDATA\app.mentora.desktop\logs\backend.log"
