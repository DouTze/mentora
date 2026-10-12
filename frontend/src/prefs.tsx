// 偏好: interface language, colour theme and the language guides are written in. Stored per browser; applied to <html> so CSS and screen readers see it.
import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";
import { I18nContext, translate, type Locale } from "./i18n";
import { useStored } from "./route";
import { syncWindowTheme } from "./desktop";

export type Theme = "system" | "light" | "dark";
/** 導讀語言: "zh" asks for guides, questions and quizzes in 繁體中文; "en" in the book's own language (English books) */
export type GuideLang = "zh" | "en";

interface Prefs {
  theme: Theme;
  setTheme: (t: Theme) => void;
  locale: Locale;
  setLocale: (l: Locale) => void;
  guideLang: GuideLang;
  setGuideLang: (g: GuideLang) => void;
}

const PrefsContext = createContext<Prefs>({ theme: "system", setTheme: () => {}, locale: "zh-Hant", setLocale: () => {}, guideLang: "zh", setGuideLang: () => {} });

function initialLocale(): Locale {
  return typeof navigator !== "undefined" && /^en\b/i.test(navigator.language) ? "en" : "zh-Hant";
}

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useStored<Theme>("mentor.theme", "system");
  const [locale, setLocale] = useStored<Locale>("mentor.locale", initialLocale());
  // until chosen in 設定 › 導讀, guides follow the interface language
  const [chosenGuide, setGuideLang] = useStored<GuideLang | null>("mentor.guideLang", null);
  const guideLang: GuideLang = chosenGuide ?? (locale === "en" ? "en" : "zh");

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
    // the desktop window's title bar follows too, also when "system" flips between light and dark
    syncWindowTheme(theme);
    const scheme = window.matchMedia?.("(prefers-color-scheme: dark)");
    const onScheme = () => syncWindowTheme(theme);
    scheme?.addEventListener("change", onScheme);
    return () => scheme?.removeEventListener("change", onScheme);
  }, [theme]);

  useEffect(() => {
    document.documentElement.lang = locale;
    document.title = translate(locale, "app.title");
  }, [locale]);

  const i18n = useMemo(() => ({ locale, t: (k: Parameters<typeof translate>[1], v?: Parameters<typeof translate>[2]) => translate(locale, k, v) }), [locale]);

  return (
    <PrefsContext.Provider value={{ theme, setTheme, locale, setLocale, guideLang, setGuideLang }}>
      <I18nContext.Provider value={i18n}>{children}</I18nContext.Provider>
    </PrefsContext.Provider>
  );
}

export const usePrefs = () => useContext(PrefsContext);
