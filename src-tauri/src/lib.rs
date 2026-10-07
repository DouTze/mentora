//! Mentora desktop shell.
//!
//! Packaged app: starts the Python backend (`binaries/mentor-backend`, built by scripts/build-sidecar.ts) on a free
//! localhost port, stores its data in the per-user app data directory, and tells the page where the backend is by
//! setting `window.__MENTOR_API__` before the page loads. The sidecar exits when we close its stdin, which happens
//! on quit and also when this process dies, so no backend is left running.
//!
//! `tauri dev`: the page comes from the bun dev server (`bun run dev`), which already proxies `/api` to a uvicorn
//! started from source, so no sidecar is started.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::webview::DownloadEvent;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// The running sidecar; taken out and killed on exit.
struct Sidecar(Mutex<Option<CommandChild>>);

fn free_port() -> std::io::Result<u16> {
    // bind port 0 → the OS picks a free port; the listener is dropped right away so the sidecar can bind it
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

fn open_log(dir: &Path) -> Option<File> {
    fs::create_dir_all(dir).ok()?;
    OpenOptions::new().create(true).append(true).open(dir.join("backend.log")).ok()
}

/// Start the backend and return the API base URL the page should use.
fn start_sidecar(app: &AppHandle) -> Result<String, Box<dyn std::error::Error>> {
    let port = free_port()?;
    let data_dir = app.path().app_data_dir()?;
    fs::create_dir_all(&data_dir)?;
    let port_arg = port.to_string();
    let data_arg = data_dir.to_string_lossy().into_owned();

    let (mut rx, child) = app
        .shell()
        .sidecar("mentor-backend")?
        .args(["--port", port_arg.as_str(), "--data-dir", data_arg.as_str(), "--watch-stdin"])
        .spawn()?;
    app.manage(Sidecar(Mutex::new(Some(child))));

    // keep the backend's output in <app log dir>/backend.log for troubleshooting
    let mut log = app.path().app_log_dir().ok().and_then(|d| open_log(&d));
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            let line = match event {
                CommandEvent::Stdout(b) | CommandEvent::Stderr(b) => String::from_utf8_lossy(&b).into_owned(),
                CommandEvent::Error(e) => format!("[sidecar error] {e}\n"),
                CommandEvent::Terminated(p) => format!("[sidecar exited] code={:?} signal={:?}\n", p.code, p.signal),
                _ => continue,
            };
            #[cfg(debug_assertions)]
            print!("[backend] {line}");
            if let Some(f) = log.as_mut() {
                let _ = f.write_all(line.as_bytes());
            }
        }
    });

    Ok(format!("http://127.0.0.1:{port}/api"))
}

fn stop_sidecar(app: &AppHandle) {
    if let Some(state) = app.try_state::<Sidecar>() {
        if let Some(child) = state.0.lock().unwrap().take() {
            // kill the PyInstaller bootloader; dropping our end of its stdin also makes the Python child exit
            let _ = child.kill();
        }
    }
}

/// `name` in `dir`, or `name (2)`, `name (3)`, … when that file already exists.
fn unique_path(dir: &Path, name: &str) -> PathBuf {
    let first = dir.join(name);
    if !first.exists() {
        return first;
    }
    let (stem, ext) = match name.rsplit_once('.') {
        Some((s, e)) if !s.is_empty() => (s.to_string(), format!(".{e}")),
        _ => (name.to_string(), String::new()),
    };
    (2..)
        .map(|n| dir.join(format!("{stem} ({n}){ext}")))
        .find(|p| !p.exists())
        .unwrap()
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let api_base = if tauri::is_dev() { None } else { Some(start_sidecar(&handle)?) };
            // Windows: the page draws its own title bar (frontend/src/components/TitleBar.tsx) so it matches the app,
            // like Word does. MENTOR_CUSTOM_TITLEBAR=1 turns it on elsewhere too, to work on it from a Mac.
            let custom_titlebar = cfg!(windows) || std::env::var_os("MENTOR_CUSTOM_TITLEBAR").is_some_and(|v| v == "1");

            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Mentora")
                .inner_size(1280.0, 860.0)
                .min_inner_size(900.0, 600.0)
                // without decorations Windows still draws the drop shadow and the rounded corners (Windows 11)
                .decorations(!custom_titlebar)
                // Tauri's own file-drop handler swallows the page's HTML5 drag events (on Windows entirely), which
                // breaks moving materials between categories; the page reads dropped files from the drop event itself
                .disable_drag_drop_handler()
                // exports (Content-Disposition: attachment) land in Downloads and are shown in Finder / Explorer
                .on_download(|webview, event| {
                    match event {
                        DownloadEvent::Requested { url, destination } => {
                            let name = destination
                                .file_name()
                                .map(|n| n.to_string_lossy().into_owned())
                                .or_else(|| url.path_segments().and_then(|mut s| s.next_back()).map(str::to_string))
                                .filter(|n| !n.is_empty())
                                .unwrap_or_else(|| "download".into());
                            if let Ok(dir) = webview.app_handle().path().download_dir() {
                                *destination = unique_path(&dir, &name);
                            }
                        }
                        DownloadEvent::Finished { path: Some(path), success: true, .. } => {
                            let _ = tauri_plugin_opener::reveal_item_in_dir(path);
                        }
                        _ => {}
                    }
                    true
                });
            // what the page needs to know before it loads; both values are plain ASCII, so their Debug form ("...") is
            // a valid JS string literal
            let mut script = format!(
                "window.__MENTOR_DESKTOP__ = {{ platform: {:?}, customTitlebar: {custom_titlebar} }};",
                std::env::consts::OS
            );
            if let Some(base) = api_base {
                script += &format!("window.__MENTOR_API__ = {base:?};");
            }
            window.initialization_script(&script).build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Mentora app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_sidecar(app);
            }
        });
}
