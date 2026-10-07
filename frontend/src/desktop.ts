// What the Tauri shell (src-tauri/src/lib.rs) tells the page before it loads. null in a plain browser.
export interface DesktopInfo {
  platform: string; // "windows" | "macos" | "linux"
  customTitlebar: boolean; // the native title bar is off and the page draws its own (TitleBar.tsx)
}

export const desktop: DesktopInfo | null = (globalThis as { __MENTOR_DESKTOP__?: DesktopInfo }).__MENTOR_DESKTOP__ ?? null;
export const customTitlebar = desktop?.customTitlebar === true;

/**
 * macOS with the native title bar: the bar is transparent (src-tauri/src/lib.rs) and shows the window's background,
 * so the window background is kept at the page's --bg and the bar's text and buttons at the chosen light / dark look.
 * Call it whenever the theme changes; `theme` "system" leaves the look to the operating system.
 */
export async function syncWindowTheme(theme: "system" | "light" | "dark"): Promise<void> {
  if (desktop?.platform !== "macos" || customTitlebar) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  const win = getCurrentWindow();
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  await Promise.all([win.setTheme(theme === "system" ? null : theme), bg ? win.setBackgroundColor(bg) : null]).catch(() => {});
}
