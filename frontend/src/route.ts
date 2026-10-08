// Hash routes, so a refresh or a shared link lands on the same page without server config.
import { useEffect, useState } from "react";

export type Route =
  | { page: "home" }
  /** `view`: the book switched to its 導讀 or 出題 of one reading, in place of the pages (the reader's 閱讀模式) */
  | { page: "material"; id: string; view?: ReaderView }
  | { page: "learn" }
  | { page: "history" };

export type ReaderView = { kind: "guide" | "quiz"; session: string };

export function parseRoute(hash: string): Route {
  const [, a, b, c, d] = hash.replace(/^#/, "").split("/");
  if (a === "m" && b) return (c === "guide" || c === "quiz") && d ? { page: "material", id: b, view: { kind: c, session: d } } : { page: "material", id: b };
  if (a === "learn" || a === "history") return { page: a };
  return { page: "home" };
}

export function toHash(r: Route): string {
  return r.page === "material" ? `#/m/${r.id}${r.view ? `/${r.view.kind}/${r.view.session}` : ""}` : r.page === "home" ? "#/" : `#/${r.page}`;
}

export function useRoute(): [Route, (r: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return [route, (r) => (location.hash = toHash(r))];
}

/** localStorage-backed state for small UI preferences (sidebar, folded categories). */
export function useStored<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  return [
    value,
    (v) => {
      setValue(v);
      try {
        localStorage.setItem(key, JSON.stringify(v));
      } catch {}
    },
  ];
}
