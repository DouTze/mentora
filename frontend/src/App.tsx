import { useEffect, useState } from "react";
import { api, RequestError } from "./api";
import type { Bookmark, Bookmarks, Library } from "./types";
import type { CompanionId } from "./companions";
import { useConnection } from "./components/CompanionDock";
import { useRoute, useStored } from "./route";
import { Sidebar } from "./components/Sidebar";
import { Toaster } from "./components/Toaster";
import { HomePage } from "./pages/Home";
import { MaterialPage, POSITION_KEY } from "./pages/Material";
import { LearnPage } from "./pages/Learn";
import { HistoryPage } from "./pages/History";
import { SettingsModal, type SettingsTab } from "./components/SettingsModal";
import { TitleBar } from "./components/TitleBar";
import { customTitlebar } from "./desktop";
import { useT } from "./i18n";
import { usePrefs } from "./prefs";

export function App() {
  const [route, navigate] = useRoute();
  const { guideSpan } = usePrefs();
  const [collapsed, setCollapsed] = useStored("mentor.sidebarCollapsed", false);
  const [conn, recheck, reportCheck] = useConnection();
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
  // the current reading ("進行中的學習") is let go when it no longer is one: deleted, or closed because its material was
  // deleted (wherever that happened, the library is read again after it)
  const forgetClosedSession = () => {
    const id = localStorage.getItem("mentor.sessionId");
    if (!id) return;
    api
      .session(id)
      .then((s) => s.status === "closed" && forgetSession())
      .catch((e: RequestError) => e.status === 404 && forgetSession());
  };
  const forgetSession = () => {
    localStorage.removeItem("mentor.sessionId");
    setSessionId(null);
  };
  // every book's bookmarks: the reader marks pages, the sidebar lists them under the book
  const [bookmarks, setBookmarks] = useState<Bookmarks>({});
  const refreshBookmarks = () => api.bookmarks().then(setBookmarks).catch(() => {});
  const setBookMarks = (id: string, marks: Bookmark[]) => setBookmarks((all) => ({ ...all, [id]: marks }));
  const refreshLibrary = () =>
    api
      .library()
      .then((l) => {
        setLibrary(l);
        forgetClosedSession();
        refreshBookmarks();   // a deleted book took its bookmarks with it
      })
      .catch(() => {});
  useEffect(() => {
    refreshCompanion();
    refreshLibrary();
  }, []);
  // a page opened from the sidebar (a bookmark): stored as where the book is read, then brought into view
  const [readingAt, setReadingAt] = useState<{ id: string; n: number } | null>(null);
  const [jump, setJump] = useState<{ id: string; n: number; k: number } | null>(null);
  const openPage = (id: string, n: number) => {
    try {
      localStorage.setItem(POSITION_KEY(id), String(n));
    } catch {}
    setJump({ id, n, k: Date.now() });
    navigate({ page: "material", id });
  };

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

  // 出題 from the reader: the learning page goes on to the questions once the first guide is shown
  const [quizFor, setQuizFor] = useState<string | null>(null);
  // a reading of a book opens in that book's reader, switched to its 導讀 (or 出題); one without a book (the demo)
  // has the learning page
  // a 導讀 of a book always opens the reader's 導讀 mode: a window of it at that window; a reading from before the
  // mode, still being read, at its first page (its window found or made there, as pressing 導讀 on that page does).
  // 出題, and a reading already past its guides, open that reading.
  const openSession = async (id: string, opts?: { quiz?: boolean; material?: string; tour?: boolean }) => {
    if (opts?.tour && opts.material) return navigate({ page: "material", id: opts.material, view: { kind: "tour", session: id } });
    const s = await api.session(id).catch(() => null);
    const material = opts?.material ?? s?.source?.material_id ?? null;
    if (material && s && !opts?.quiz) {
      if (s.workflow?.waiting === "tour") return navigate({ page: "material", id: material, view: { kind: "tour", session: id } });
      const start = s.source?.start;
      if (start && s.state.phase === "reading" && !s.reading?.done) {
        const v = await api.tourOpen(material, start, guideSpan).catch(() => null);
        if (v?.focus) return navigate({ page: "material", id: material, view: { kind: "tour", session: v.focus.session_id, at: start } });
      }
    }
    localStorage.setItem("mentor.sessionId", id);
    setSessionId(id);
    if (material) return navigate({ page: "material", id: material, view: { kind: opts?.quiz ? "quiz" : "guide", session: id } });
    setQuizFor(opts?.quiz ? id : null);
    navigate({ page: "learn" });
  };

  return (
    <>
    {customTitlebar && <TitleBar title={pageTitle} collapsed={collapsed} />}
    <div className={`shell ${collapsed ? "collapsed" : ""} ${route.page === "material" ? "reading" : ""}`}>
      <Sidebar
        library={library}
        route={route}
        companion={companion}
        conn={conn}
        bookmarks={bookmarks}
        readingAt={readingAt}
        onOpenPage={openPage}
        onRenameBookmark={(id, n, label) => api.renameBookmark(id, n, label).then((m) => setBookMarks(id, m)).catch(() => {})}
        onRemoveBookmark={(id, n) => api.removeBookmark(id, n).then((m) => setBookMarks(id, m)).catch(() => {})}
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
          <MaterialPage
            id={route.id}
            view={route.view ?? null}
            library={library}
            onLibraryChange={refreshLibrary}
            onNavigate={navigate}
            onOpenSession={openSession}
            bookmarks={bookmarks[route.id] ?? []}
            onBookmarks={setBookMarks}
            jump={jump}
            onPosition={(id, n) => setReadingAt((p) => (p?.id === id && p.n === n ? p : { id, n }))}
          />
        )}
        {route.page === "learn" && (
          <div className="doc">
            <LearnPage sessionId={sessionId} onSessionChange={openSession} autoQuiz={!!sessionId && quizFor === sessionId} onAutoQuizDone={() => setQuizFor(null)} onOpenMaterial={(id) => navigate({ page: "material", id })} />
          </div>
        )}
        {route.page === "history" && (
          <div className="doc wide">
            <HistoryPage onOpen={openSession} />
          </div>
        )}
      </main>
      <Toaster />
      {settingsTab && (
        <SettingsModal
          initialTab={settingsTab}
          onClose={() => {
            setSettingsTab(null);
            setHomeKey((k) => k + 1);
          }}
          onBackendChanged={refreshBackend}
          onChecked={reportCheck}
        />
      )}
    </div>
    </>
  );
}
