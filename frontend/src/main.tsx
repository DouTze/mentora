import { createRoot } from "react-dom/client";
import { App } from "./App";
import { PrefsProvider } from "./prefs";
import { customTitlebar } from "./desktop";

// the page draws the window title bar (desktop app on Windows); styles.css makes room for it
if (customTitlebar) document.documentElement.classList.add("has-titlebar");

createRoot(document.getElementById("root")!).render(
  <PrefsProvider>
    <App />
  </PrefsProvider>,
);
