# Capture this machine's screen to a PNG so the dev machine can see the app (scripts/win-remote.sh shot).
# The capture has to run inside the desktop session; from SSH it is handed over through a scheduled task.
#   screenshot.ps1 [-Out <path.png>]    prints the path of the PNG it wrote
param(
  [string]$Out = "$env:TEMP\mentora-screenshot.png",
  [switch]$Capture # internal: do the capture in this process (set when already on the desktop)
)
. "$PSScriptRoot\common.ps1"

if ($Capture) {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  # without DPI awareness a scaled display is captured at its logical size and cut off
  Add-Type -Name Dpi -Namespace Native -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
  [Native.Dpi]::SetProcessDPIAware() | Out-Null
  $b = [Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object Drawing.Bitmap $b.Width, $b.Height
  $g = [Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
  $bmp.Save("$Out.part", [Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
  Move-Item "$Out.part" $Out -Force # the caller polls for $Out, so it must appear only when complete
  return
}

Remove-Item $Out -ErrorAction SilentlyContinue
Start-HiddenScriptOnDesktop -Script $PSCommandPath -Arguments "-Capture -Out `"$Out`""
$deadline = (Get-Date).AddSeconds(20)
while (-not (Test-Path $Out) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
if (-not (Test-Path $Out)) { throw 'Screenshot timed out (is the screen locked or the display asleep?)' }
$Out
