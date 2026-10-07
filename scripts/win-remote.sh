#!/usr/bin/env bash
# Drive the Windows test machine over SSH from the dev machine (macOS/Linux):
# build the current commit there, open the desktop app on its screen, then look at it.
# The Windows side is prepared once with scripts/windows/setup-ssh-host.ps1.
#
#   scripts/win-remote.sh deploy [flags]     build HEAD (it must be pushed to origin) and open the app
#                                            flags go to deploy.ps1: -Install  -NoBuild  -NoLaunch
#   scripts/win-remote.sh shot [file.png]    screenshot of the Windows desktop (default win-shot.png)
#   scripts/win-remote.sh logs [lines]       tail of the desktop app's backend.log
#   scripts/win-remote.sh stop               quit the app
#   scripts/win-remote.sh ssh                interactive PowerShell on the Windows machine
#   scripts/win-remote.sh authorize [key.pub] log in with the Windows password once, authorize this machine's key
#                                            (default ~/.ssh/id_ed25519.pub) and turn password login off
#
# Settings come from the environment or from win-remote.env in the repo root (gitignored):
#   WIN_HOST=k4029@192.168.20.39
#   WIN_REPO=C:/Users/k4029/Documents/workspace/private/ai-programs/hackathon/asus/mentora
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/win-remote.env" ] && . "$ROOT/win-remote.env"
: "${WIN_HOST:?set WIN_HOST (e.g. user@192.168.20.39) in the environment or win-remote.env}"
: "${WIN_REPO:?set WIN_REPO (the repo path on Windows, with forward slashes) in the environment or win-remote.env}"

# Remote commands run in Windows PowerShell (the DefaultShell set by setup-ssh-host.ps1); without UTF-8 output
# its messages arrive in the Windows code page and show up garbled here
UTF8='[Console]::OutputEncoding = [Text.Encoding]::UTF8;'
ps_file() { ssh "$WIN_HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File '$WIN_REPO/scripts/windows/$1' ${*:2}"; }

cmd="${1:-}"; shift || true
case "$cmd" in
  deploy)
    # The Windows machine fetches the commit from origin: git over this SSH connection cannot reach the
    # scoop-installed git there (see scripts/windows/common.ps1)
    [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ] && echo "note: uncommitted changes are not deployed" >&2
    sha="$(git -C "$ROOT" rev-parse HEAD)"
    git -C "$ROOT" fetch -q origin
    if [ -z "$(git -C "$ROOT" branch -r --contains "$sha")" ]; then
      echo "HEAD ${sha:0:7} is not on origin yet; push it first (git push)" >&2; exit 1
    fi
    ps_file deploy.ps1 -Ref "$sha" -Fetch "$@"
    ;;
  shot)
    out="${1:-win-shot.png}"
    remote="$(ps_file screenshot.ps1 | tr -d '\r' | tail -n 1)"
    scp -q "$WIN_HOST:${remote//\\//}" "$out"
    echo "$out"
    if command -v open >/dev/null; then open "$out"; elif command -v xdg-open >/dev/null; then xdg-open "$out"; fi
    ;;
  logs)
    ssh "$WIN_HOST" "$UTF8 Get-Content \"\$env:LOCALAPPDATA\\app.mentora.desktop\\logs\\backend.log\" -Encoding UTF8 -Tail ${1:-80}"
    ;;
  stop)
    ssh "$WIN_HOST" "Get-Process -Name mentora, mentor-backend -ErrorAction SilentlyContinue | Stop-Process -Force"
    ;;
  ssh)
    ssh -t "$WIN_HOST"
    ;;
  authorize)
    pub="${1:-$HOME/.ssh/id_ed25519.pub}"
    [ -f "$pub" ] || { echo "no $pub; create one with: ssh-keygen -t ed25519" >&2; exit 1; }
    # password login only for this call; sshd restarts at the end, so the connection may drop after the key is in.
    # Not possible when the Windows account only allows Windows Hello sign-in (see docs/windows-remote-test.md).
    ssh -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no "$WIN_HOST" \
      "powershell -NoProfile -ExecutionPolicy Bypass -File '$WIN_REPO/scripts/windows/setup-ssh-host.ps1' -PublicKey \"$(head -n 1 "$pub")\" -DisablePasswordAuth" || true
    ssh -o BatchMode=yes "$WIN_HOST" "Write-Output \"key login ok: \$env:COMPUTERNAME\""
    ;;
  *)
    awk 'NR > 1 && /^set -e/ { exit } NR > 1' "$0"; exit 1
    ;;
esac
