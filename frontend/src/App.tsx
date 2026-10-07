import { useEffect, useState } from "react";
import { api } from "./api";
import type { ActiveBackend, Library } from "./types";
import { useRoute, useStored } from "./route";
import { Sidebar } from "./components/Sidebar";
import { HomePage } from "./pages/Home";
import { MaterialPage } from "./pages/Material";
import { LearnPage } from "./pages/Learn";
import { HistoryPage } from "./pages/History";
import { SettingsModal } from "./components/SettingsModal";
import { TitleBar } from "./components/TitleBar";
import { customTitlebar } from "./desktop";
import { useT } from "./i18n";

export function App() {
  const [route, navigate] = useRoute();
  const [collapsed, setCollapsed] = useStored("mentor.sidebarCollapsed", false);
  const [active, setActive] = useState<ActiveBackend | null>(null);
  const [library, setLibrary] = useState<Library>({ categories: [], materials: [] });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(() => localStorage.getItem("mentor.sessionId"));

  const refreshBackend = () => api.health().then((h) => setActive(h.backend)).catch(() => setActive(null));
  const refreshLibrary = () => api.library().then(setLibrary).catch(() => {});
  useEffect(() => {
    refreshBackend();
    refreshLibrary();
  }, []);

  // Cmd/Ctrl + \ folds the sidebar, same as Notion
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        e.preventDefault();
        setCollapsed(!collapsed);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [collapsed]);

  const t = useT();
  const pageTitle =
    route.page === "material"
      ? (library.materials.find((m) => m.id === route.id)?.title ?? "")
      : t(route.page === "learn" ? "nav.learn" : route.page === "history" ? "nav.history" : "nav.home");

  const openSession = (id: string) => {
    localStorage.setItem("mentor.sessionId", id);
    setSessionId(id);
    navigate({ page: "learn" });
  };

  return (
    <>
    {customTitlebar && <TitleBar title={pageTitle} collapsed={collapsed} />}
    <div className={`shell ${collapsed ? "collapsed" : ""}`}>
      <Sidebar
        library={library}
        route={route}
        active={active}
        hasSession={!!sessionId}
        collapsed={collapsed}
        onToggle={() => setCollapsed(!collapsed)}
        onNavigate={navigate}
        onLibraryChange={refreshLibrary}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      <main className="main">
        {route.page === "home" && <HomePage library={library} onLibraryChange={refreshLibrary} onNavigate={navigate} onOpenSession={openSession} />}
        {route.page === "material" && (
          <MaterialPage id={route.id} library={library} onLibraryChange={refreshLibrary} onNavigate={navigate} onOpenSession={openSession} />
        )}
        {route.page === "learn" && (
          <div className="doc">
            <LearnPage sessionId={sessionId} onSessionChange={openSession} onOpenMaterial={(id) => navigate({ page: "material", id })} />
          </div>
        )}
        {route.page === "history" && (
          <div className="doc wide">
            <HistoryPage onOpen={openSession} />
          </div>
        )}
      </main>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} onBackendChanged={refreshBackend} />}
    </div>
    </>
  );
}
