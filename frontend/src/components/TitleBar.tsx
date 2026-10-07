// The window's title bar in the desktop app on Windows (the native one is turned off), drawn like Word's: same
// colours as the app, the sidebar column carried up to the top edge, the page title in the middle and the
// system-style caption buttons on the right. Empty areas drag the window; double-click maximizes (Tauri's
// data-tauri-drag-region, which only reacts on the element that carries it, so the text inside ignores the pointer).
import { useEffect, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useT } from "../i18n";
import appIcon from "../assets/app-icon.png";

const APP_NAME = "Mentora";

// Segoe Fluent Icons (Windows 11) / Segoe MDL2 Assets (Windows 10) glyphs, the ones Windows' own caption buttons use
const GLYPH = { minimize: "\uE921", maximize: "\uE922", restore: "\uE923", close: "\uE8BB" };

export function TitleBar({ title, collapsed }: { title: string; collapsed: boolean }) {
  const t = useT();
  const [maximized, setMaximized] = useState(false);

  useEffect(() => {
    const win = getCurrentWindow();
    const sync = () => win.isMaximized().then(setMaximized).catch(() => {});
    sync();
    const unlisten = win.onResized(sync);
    return () => {
      unlisten.then((off) => off());
    };
  }, []);

  // the taskbar and Alt-Tab show the native window title
  useEffect(() => {
    getCurrentWindow()
      .setTitle(title ? `${title} - ${APP_NAME}` : APP_NAME)
      .catch(() => {});
  }, [title]);

  const win = () => getCurrentWindow();
  return (
    <header className={`titlebar ${collapsed ? "collapsed" : ""}`} data-tauri-drag-region>
      <div className="tb-side" data-tauri-drag-region>
        <img className="tb-logo" src={appIcon} alt="" />
        <span className="tb-app">{APP_NAME}</span>
      </div>
      <div className="tb-main" data-tauri-drag-region>
        <span className="tb-title">{title}</span>
      </div>
      <div className="tb-controls">
        <button type="button" className="tb-btn" aria-label={t("titlebar.minimize")} title={t("titlebar.minimize")} onClick={() => win().minimize()}>
          {GLYPH.minimize}
        </button>
        <button
          type="button"
          className="tb-btn"
          aria-label={t(maximized ? "titlebar.restore" : "titlebar.maximize")}
          title={t(maximized ? "titlebar.restore" : "titlebar.maximize")}
          onClick={() => win().toggleMaximize()}
        >
          {maximized ? GLYPH.restore : GLYPH.maximize}
        </button>
        <button type="button" className="tb-btn close" aria-label={t("titlebar.close")} title={t("titlebar.close")} onClick={() => win().close()}>
          {GLYPH.close}
        </button>
      </div>
    </header>
  );
}
