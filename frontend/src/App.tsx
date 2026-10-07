import { useEffect, useState } from "react";
import { api } from "./api";
import type { Library } from "./types";
import type { CompanionId } from "./companions";
import { useConnection } from "./components/CompanionDock";
import { useRoute, useStored } from "./route";
import { Sidebar } from "./components/Sidebar";
import { HomePage } from "./pages/Home";
import { MaterialPage } from "./pages/Material";
import { LearnPage } from "./pages/Learn";
import { HistoryPage } from "./pages/History";
import { SettingsModal, type SettingsTab } from "./components/SettingsModal";
import { TitleBar } from "./components/TitleBar";
import { customTitlebar } from "./desktop";
import { useT } from "./i18n";

export function App() {
  const [route, navigate] = useRoute();
  const [collapsed, setCollapsed] = useStored("mentor.sidebarCollapsed", false);
  const [conn, recheck] = useConnection();
  const [companion, setCompanion] = useState<CompanionId>("owl");
  const [library, setLibrary] = useState<Library>({ categories: [], materials: [] });
  const [settingsTab, setSettingsTab] = useState<SettingsTab | null>(null);
  // the home page reads the shelf again after settings close: its scope may have changed there
  const [homeKey, setHomeKey] = useState(0);
  const [sessionId, setSessionId] = useState<string | null>(() => localStorage.getItem("mentor.sessionId"));

  const refreshCompanion = () => api.companions().then((c) => c && setCompanion(c.active)).catch(() => {});
  // the settings changed the companion or its model: show who it is now and ask again whether it answers
  const refreshBackend = () => {
    refreshCompanion();
    recheck();
  };
  const refreshLibrary = () => api.library().then(setLibrary).catch(() => {});
  useEffect(() => {
    refreshCompanion();
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
        companion={companion}
        conn={conn}
        hasSession={!!sessionId}
        collapsed={collapsed}
        onToggle={() => setCollapsed(!collapsed)}
        onNavigate={navigate}
        onLibraryChange={refreshLibrary}
        onOpenSettings={() => setSettingsTab("prefs")}
      />
      <main className="main">
        {route.page === "home" && (
          <HomePage key={homeKey} library={library} onLibraryChange={refreshLibrary} onNavigate={navigate} onOpenSession={openSession} onOpenScope={() => setSettingsTab("shelf")} />
        )}
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
      {settingsTab && (
        <SettingsModal
          initialTab={settingsTab}
          onClose={() => {
            setSettingsTab(null);
            setHomeKey((k) => k + 1);
          }}
          onBackendChanged={refreshBackend}
        />
      )}
    </div>
    </>
  );
}
