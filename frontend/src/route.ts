// Hash routes, so a refresh or a shared link lands on the same page without server config.
import { useEffect, useState } from "react";

export type Route =
  | { page: "home" }
  | { page: "material"; id: string }
  | { page: "learn" }
  | { page: "history" };

export function parseRoute(hash: string): Route {
  const [, a, b] = hash.replace(/^#/, "").split("/");
  if (a === "m" && b) return { page: "material", id: b };
  if (a === "learn" || a === "history") return { page: a };
  return { page: "home" };
}

export function toHash(r: Route): string {
  return r.page === "material" ? `#/m/${r.id}` : r.page === "home" ? "#/" : `#/${r.page}`;
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
