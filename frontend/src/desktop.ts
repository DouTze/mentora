// What the Tauri shell (src-tauri/src/lib.rs) tells the page before it loads. null in a plain browser.
export interface DesktopInfo {
  platform: string; // "windows" | "macos" | "linux"
  customTitlebar: boolean; // the native title bar is off and the page draws its own (TitleBar.tsx)
}

export const desktop: DesktopInfo | null = (globalThis as { __MENTOR_DESKTOP__?: DesktopInfo }).__MENTOR_DESKTOP__ ?? null;
export const customTitlebar = desktop?.customTitlebar === true;
