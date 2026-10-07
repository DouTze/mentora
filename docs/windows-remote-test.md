# 用 SSH 在 Windows 測試機上部署桌面版

開發在 macOS／Linux 上進行，Windows 版的桌面 App（PyInstaller sidecar 不能跨平台編譯）交給一台 Windows 測試機：開發機把 commit 推到 GitHub 後，透過 SSH 讓那台機器取得該 commit、打包並開啟 App，再用截圖與 log 檢查執行效果。

```
開發機                                   Windows 測試機
git push（GitHub）
scripts/win-remote.sh deploy  ──ssh──────▶  scripts/windows/deploy.ps1 -Ref <HEAD> -Fetch
                                              （在桌面工作階段）fetch → checkout → tauri build → 開啟 App
scripts/win-remote.sh shot    ──ssh/scp──▶  scripts/windows/screenshot.ps1
```

## Windows 測試機（一次性）

1. 需要 `git`、`bun`、`uv`、Rust（`rustup`，MSVC 工具鏈）與 Visual Studio Build Tools（C++ 工作負載）；WebView2 在 Windows 11 內建。用 scoop 的話：`scoop install git bun uv rustup`，再 `rustup default stable`。
2. 在測試機上以**系統管理員身分**開 PowerShell 執行（整個流程中唯一需要人在測試機前的一步：安裝 SSH 伺服器要按 UAC 的「是」）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\setup-ssh-host.ps1 -NoSleepOnAC
   ```

   這會安裝並啟動 OpenSSH Server（開機自動啟動）、開放 22 port、讓 SSH 預設使用 PowerShell。`-NoSleepOnAC` 讓插電時不進入睡眠，機器才連得到。有些 Windows 版本要重新開機才會裝好 OpenSSH Server：腳本會提示，並註冊一個開機時以 SYSTEM 執行的一次性工作，重新啟動（不是關機）後自動完成其餘設定，不必再登入或按 UAC（紀錄在 `C:\ProgramData\ssh\mentora-setup.log`）。
3. 授權開發機的公鑰，二擇一：
   - 帳號可以用密碼登入時：在開發機設定好下面的 `win-remote.env`，執行 `scripts/win-remote.sh authorize`。輸入一次 Windows 密碼（Microsoft 帳號要用帳號密碼，不是 PIN），它會授權開發機的公鑰（預設 `~/.ssh/id_ed25519.pub`）並**關閉密碼登入**。Administrators 帳號從 SSH 登入時已經是管理員權限，所以這一步不需要 UAC。
   - Microsoft 帳號開了「只允許使用 Windows Hello 登入」時，Windows 不接受密碼登入（密碼正確仍然 `Permission denied`）：把開發機的公鑰交給測試機，在測試機上以系統管理員身分執行 `setup-ssh-host.ps1 -PublicKey "<公鑰>" -DisablePasswordAuth`。

   公鑰存在 `C:\ProgramData\ssh\administrators_authorized_keys`（不是 `~\.ssh\authorized_keys`）。之後要加別台開發機的金鑰，用已授權的機器連進去再跑一次 `setup-ssh-host.ps1 -PublicKey "<公鑰>"`。
4. 測試機要保持**登入桌面**（鎖定畫面可以部署與開 App，但截圖會失敗；登出則無法部署）。
5. 測試機的 repo 要能不經互動地 `git fetch origin`（在測試機上 fetch 過一次、憑證存進 Git Credential Manager 即可）。

不在同一個區網時，可在兩台機器都登入 Tailscale，改用 Tailscale 的 IP（公司網路可能擋掉 Tailscale）。

## 開發機

在 repo 根目錄建立 `win-remote.env`（已列入 `.gitignore`）：

```bash
WIN_HOST=k4029@192.168.20.39
WIN_REPO=C:/Users/k4029/Documents/workspace/private/ai-programs/hackathon/asus/mentora
```

```bash
git push                                # deploy 只接受已推到 GitHub 的 commit
scripts/win-remote.sh deploy            # 測試機 fetch HEAD → 打包 → 在桌面開啟 target\release\mentora.exe
scripts/win-remote.sh deploy -Install   # 改打包 NSIS 安裝檔、靜默安裝，再開啟安裝後的 App（測安裝流程）
scripts/win-remote.sh deploy -NoBuild   # 不重新打包，只重新開啟
scripts/win-remote.sh shot              # 截下測試機整個桌面，存成 win-shot.png 並開啟
scripts/win-remote.sh logs 200          # 看後端 log（%LOCALAPPDATA%\app.mentora.desktop\logs\backend.log）
scripts/win-remote.sh stop              # 關閉 App
scripts/win-remote.sh ssh               # 進測試機的 PowerShell
```

- 只會部署**已推到 GitHub** 的 commit：`deploy` 把 HEAD 的 commit 傳給測試機，測試機 `git fetch origin` 後以 detached HEAD 切過去，不會動到測試機上的分支。測試機的工作目錄有未 commit 的變更時會拒絕切換。
- 第一次打包要下載 Python、Rust crates 並編譯，需要數分鐘；之後是增量建置。打包在測試機的桌面工作階段裡進行，SSH 斷線也不會中止（輸出記在測試機的 `%TEMP%\mentora-remote-deploy.log`）。
- 為什麼要繞到桌面工作階段：SSH 啟動的程式跑在看不到的工作階段裡，App 不會出現在螢幕上；而且 Administrators 帳號的 SSH 連線是提升權限的，提升權限的程式不會跟隨一般使用者建立的 junction（scoop 的 `apps\<app>\current`、uv 的 Python 連結），git、bun、uv、cargo 都會「找不到」。所以 `deploy.ps1` 偵測到提升權限時，會透過排程工作（`\mentora\` 資料夾，只在使用者登入時執行）在使用者桌面上以一般權限重跑自己，並把輸出即時傳回；`screenshot.ps1` 也以同樣方式截圖。同理，git 無法經由 SSH 直接 push 到測試機。
- 連線逾時（`ssh`、`nc`、`ping` 都沒回應），而測試機的 sshd 紀錄（事件檢視器 → OpenSSH/Operational）看不到任何連線：多半是 macOS 的「區域網路」權限沒開。macOS 15 起每個 App 都要有這個權限才能連區網裝置，否則連線會被默默丟掉。到 系統設定 → 隱私權與安全性 → 區域網路，打開你執行指令的 App（終端機、iTerm、VS Code…），再重新開啟該 App。
- Windows 11 Home 沒有遠端桌面伺服器；要即時操作畫面，可另外裝 RustDesk、Chrome 遠端桌面等工具，或直接在測試機前操作。
