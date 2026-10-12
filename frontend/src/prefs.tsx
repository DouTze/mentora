// 偏好: interface language, colour theme and the language guides are written in. Stored per browser; applied to <html> so CSS and screen readers see it.
import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";
import { I18nContext, translate, type Locale } from "./i18n";
import { useStored } from "./route";
import { syncWindowTheme } from "./desktop";

export type Theme = "system" | "light" | "dark";
/** 導讀語言: "zh" asks for guides, questions and quizzes in 繁體中文; "en" in the book's own language (English books) */
export type GuideLang = "zh" | "en";
/** 導讀範圍: the pages taken on either side of the page being read when 導讀 is pressed */
export const GUIDE_SPANS = [0, 1, 2, 3, 4, 5] as const;
export const DEFAULT_GUIDE_SPAN = 2;

interface Prefs {
  theme: Theme;
  setTheme: (t: Theme) => void;
  locale: Locale;
  setLocale: (l: Locale) => void;
  guideLang: GuideLang;
  setGuideLang: (g: GuideLang) => void;
  guideSpan: number;
  setGuideSpan: (n: number) => void;
}

const PrefsContext = createContext<Prefs>({ theme: "system", setTheme: () => {}, locale: "zh-Hant", setLocale: () => {}, guideLang: "zh", setGuideLang: () => {}, guideSpan: DEFAULT_GUIDE_SPAN, setGuideSpan: () => {} });

function initialLocale(): Locale {
  return typeof navigator !== "undefined" && /^en\b/i.test(navigator.language) ? "en" : "zh-Hant";
}

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useStored<Theme>("mentor.theme", "system");
  const [locale, setLocale] = useStored<Locale>("mentor.locale", initialLocale());
  // until chosen in 設定 › 導讀, guides follow the interface language
  const [chosenGuide, setGuideLang] = useStored<GuideLang | null>("mentor.guideLang", null);
  const guideLang: GuideLang = chosenGuide ?? (locale === "en" ? "en" : "zh");
  const [storedSpan, setGuideSpan] = useStored<number>("mentor.guideSpan", DEFAULT_GUIDE_SPAN);
  const guideSpan = (GUIDE_SPANS as readonly number[]).includes(storedSpan) ? storedSpan : DEFAULT_GUIDE_SPAN;

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
    <PrefsContext.Provider value={{ theme, setTheme, locale, setLocale, guideLang, setGuideLang, guideSpan, setGuideSpan }}>
      <I18nContext.Provider value={i18n}>{children}</I18nContext.Provider>
    </PrefsContext.Provider>
  );
}

export const usePrefs = () => useContext(PrefsContext);
