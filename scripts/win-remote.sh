#!/usr/bin/env bash
# Drive the Windows test machine over SSH from the dev machine (macOS/Linux):
# push the current commit there, build and open the desktop app, then look at it.
# The Windows side is prepared once with scripts/windows/setup-ssh-host.ps1.
#
#   scripts/win-remote.sh authorize [key.pub] log in with the Windows password once, authorize this machine's key
#                                            (default ~/.ssh/id_ed25519.pub) and turn password login off
#   scripts/win-remote.sh init               add the git remote "win" pointing at the repo on the Windows machine
#   scripts/win-remote.sh deploy [flags]     push HEAD to win-deploy there, build, open the app
#                                            flags go to deploy.ps1: -Install  -NoBuild  -NoLaunch
#   scripts/win-remote.sh shot [file.png]    screenshot of the Windows desktop (default win-shot.png)
#   scripts/win-remote.sh logs [lines]       tail of the desktop app's backend.log
#   scripts/win-remote.sh stop               quit the app
#   scripts/win-remote.sh ssh                interactive PowerShell on the Windows machine
#
# Settings come from the environment or from win-remote.env in the repo root (gitignored):
#   WIN_HOST=k4029@192.168.20.39
#   WIN_REPO=C:/Users/k4029/Documents/workspace/private/ai-programs/hackathon/asus/mentora
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/win-remote.env" ] && . "$ROOT/win-remote.env"
: "${WIN_HOST:?set WIN_HOST (e.g. user@192.168.20.39) in the environment or win-remote.env}"
: "${WIN_REPO:?set WIN_REPO (the repo path on Windows, with forward slashes) in the environment or win-remote.env}"

# Remote commands run in Windows PowerShell (the DefaultShell set by setup-ssh-host.ps1)
ps_file() { ssh "$WIN_HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File '$WIN_REPO/scripts/windows/$1' ${*:2}"; }

cmd="${1:-}"; shift || true
case "$cmd" in
  authorize)
    pub="${1:-$HOME/.ssh/id_ed25519.pub}"
    [ -f "$pub" ] || { echo "no $pub; create one with: ssh-keygen -t ed25519" >&2; exit 1; }
    # password login only for this call; sshd restarts at the end, so the connection may drop after the key is in
    ssh -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no "$WIN_HOST" \
      "powershell -NoProfile -ExecutionPolicy Bypass -File '$WIN_REPO/scripts/windows/setup-ssh-host.ps1' -PublicKey \"$(head -n 1 "$pub")\" -DisablePasswordAuth" || true
    ssh -o BatchMode=yes "$WIN_HOST" "Write-Output \"key login ok: \$env:COMPUTERNAME\""
    ;;
  init)
    git -C "$ROOT" remote remove win 2>/dev/null || true
    git -C "$ROOT" remote add win "$WIN_HOST:$WIN_REPO"
    # git-receive-pack / git-upload-pack are not on PATH on Windows; the git subcommands are
    git -C "$ROOT" config remote.win.receivepack "git receive-pack"
    git -C "$ROOT" config remote.win.uploadpack "git upload-pack"
    ssh "$WIN_HOST" "git -C '$WIN_REPO' log -1 --oneline"
    echo "remote 'win' ready"
    ;;
  deploy)
    # only committed work is deployed; the Windows checkout is detached at win-deploy
    [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ] && echo "note: uncommitted changes are not deployed" >&2
    git -C "$ROOT" push --force win HEAD:refs/heads/win-deploy
    ps_file deploy.ps1 -Ref win-deploy "$@"
    ;;
  shot)
    out="${1:-win-shot.png}"
    remote="$(ps_file screenshot.ps1 | tr -d '\r' | tail -n 1)"
    scp -q "$WIN_HOST:${remote//\\//}" "$out"
    echo "$out"
    if command -v open >/dev/null; then open "$out"; elif command -v xdg-open >/dev/null; then xdg-open "$out"; fi
    ;;
  logs)
    ssh "$WIN_HOST" "Get-Content \"\$env:LOCALAPPDATA\\app.mentora.desktop\\logs\\backend.log\" -Tail ${1:-80}"
    ;;
  stop)
    ssh "$WIN_HOST" "Get-Process -Name mentora, mentor-backend -ErrorAction SilentlyContinue | Stop-Process -Force"
    ;;
  ssh)
    ssh -t "$WIN_HOST"
    ;;
  *)
    awk 'NR > 1 && /^set -e/ { exit } NR > 1' "$0"; exit 1
    ;;
esac
