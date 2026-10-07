# 用 SSH 在 Windows 測試機上部署桌面版

開發在 macOS／Linux 上進行，Windows 版的桌面 App（PyInstaller sidecar 不能跨平台編譯）交給一台 Windows 測試機：開發機透過 SSH 把目前的 commit 推過去、在那台機器上打包並開啟 App，再用截圖與 log 檢查執行效果。

```
開發機                                   Windows 測試機
scripts/win-remote.sh deploy  ──git push──▶  refs/heads/win-deploy
                              ──ssh──────▶  scripts/windows/deploy.ps1
                                              checkout → tauri build → 在桌面開啟 App
scripts/win-remote.sh shot    ──ssh/scp──▶  scripts/windows/screenshot.ps1
```

## Windows 測試機（一次性）

1. 需要 `git`、`bun`、`uv`、Rust（`rustup`，MSVC 工具鏈）與 Visual Studio Build Tools（C++ 工作負載）；WebView2 在 Windows 11 內建。用 scoop 的話：`scoop install git bun uv rustup`，再 `rustup default stable`。
2. 在測試機上以**系統管理員身分**開 PowerShell 執行（整個流程中唯一需要人在測試機前的一步：安裝 SSH 伺服器要按 UAC 的「是」）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\setup-ssh-host.ps1 -NoSleepOnAC
   ```

   這會安裝並啟動 OpenSSH Server（開機自動啟動）、開放 22 port、讓 SSH 預設使用 PowerShell。此時可以用這個 Windows 帳號的密碼登入（Microsoft 帳號要用帳號密碼，不是 PIN）。`-NoSleepOnAC` 讓插電時不進入睡眠，機器才連得到。
3. 回到開發機，設定好下面的 `win-remote.env` 後執行 `scripts/win-remote.sh authorize`：輸入一次 Windows 密碼，它會授權開發機的公鑰（預設 `~/.ssh/id_ed25519.pub`）並**關閉密碼登入**，之後只能用金鑰連線。Administrators 群組的帳號從 SSH 登入時已經是管理員權限，所以這一步不需要 UAC。公鑰存在 `C:\ProgramData\ssh\administrators_authorized_keys`（不是 `~\.ssh\authorized_keys`）。之後要加別台開發機的金鑰，用已授權的機器連進去再跑一次 `setup-ssh-host.ps1 -PublicKey "<公鑰>"`。
4. 測試機要保持**登入桌面**（鎖定畫面可以開 App，但截圖會失敗；登出則 App 無法顯示）。

不在同一個區網時，可在兩台機器都登入 Tailscale，改用 Tailscale 的 IP。

## 開發機

在 repo 根目錄建立 `win-remote.env`（已列入 `.gitignore`）：

```bash
WIN_HOST=k4029@192.168.20.39
WIN_REPO=C:/Users/k4029/Documents/workspace/private/ai-programs/hackathon/asus/mentora
```

```bash
scripts/win-remote.sh authorize         # 第一次：用密碼登入、授權本機金鑰、關閉密碼登入
scripts/win-remote.sh init              # 加入 git remote「win」，順便確認 SSH 連得上
scripts/win-remote.sh deploy            # 推送 HEAD → 打包 → 在測試機桌面開啟 target\release\mentora.exe
scripts/win-remote.sh deploy -Install   # 改打包 NSIS 安裝檔、靜默安裝，再開啟安裝後的 App（測安裝流程）
scripts/win-remote.sh deploy -NoBuild   # 不重新打包，只重新開啟
scripts/win-remote.sh shot              # 截下測試機整個桌面，存成 win-shot.png 並開啟
scripts/win-remote.sh logs 200          # 看後端 log（%LOCALAPPDATA%\app.mentora.desktop\logs\backend.log）
scripts/win-remote.sh stop              # 關閉 App
scripts/win-remote.sh ssh               # 進測試機的 PowerShell
```

- 只會部署**已 commit** 的內容：`deploy` 把 HEAD 強制推到測試機的 `win-deploy` 分支，測試機以 detached HEAD 切到那裡，所以不需要先推到 GitHub，也不會動到測試機上的其他分支。測試機的工作目錄有未 commit 的變更時會拒絕切換。
- 測試機的 repo 是從 GitHub 拉下來的那份；要直接用 GitHub 上的分支：`ssh $WIN_HOST "powershell -ExecutionPolicy Bypass -File $WIN_REPO/scripts/windows/deploy.ps1 -Ref origin/develop -Fetch"`（需要測試機能不經互動取得 GitHub 憑證，私有 repo 建議用上面的 push 方式）。
- 第一次打包要下載 Python、Rust crates 並編譯，需要數分鐘；之後是增量建置。SSH 斷線會中止打包，重跑即可。
- SSH 啟動的程式跑在看不到的工作階段裡，所以 `deploy.ps1` 與 `screenshot.ps1` 透過排程工作（`\mentora\` 資料夾，只在使用者登入時執行）在使用者的桌面上啟動 App 與截圖。
- Windows 11 Home 沒有遠端桌面伺服器；要即時操作畫面，可另外裝 RustDesk、Chrome 遠端桌面等工具，或直接在測試機前操作。
