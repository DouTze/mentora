// 偏好: interface language and colour theme. Stored per browser; applied to <html> so CSS and screen readers see it.
import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";
import { I18nContext, translate, type Locale } from "./i18n";
import { useStored } from "./route";

export type Theme = "system" | "light" | "dark";

interface Prefs {
  theme: Theme;
  setTheme: (t: Theme) => void;
  locale: Locale;
  setLocale: (l: Locale) => void;
}

const PrefsContext = createContext<Prefs>({ theme: "system", setTheme: () => {}, locale: "zh-Hant", setLocale: () => {} });

function initialLocale(): Locale {
  return typeof navigator !== "undefined" && /^en\b/i.test(navigator.language) ? "en" : "zh-Hant";
}

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useStored<Theme>("mentor.theme", "system");
  const [locale, setLocale] = useStored<Locale>("mentor.locale", initialLocale());

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    document.documentElement.lang = locale;
    document.title = translate(locale, "app.title");
  }, [locale]);

  const i18n = useMemo(() => ({ locale, t: (k: Parameters<typeof translate>[1], v?: Parameters<typeof translate>[2]) => translate(locale, k, v) }), [locale]);

  return (
    <PrefsContext.Provider value={{ theme, setTheme, locale, setLocale }}>
      <I18nContext.Provider value={i18n}>{children}</I18nContext.Provider>
    </PrefsContext.Provider>
  );
}

export const usePrefs = () => useContext(PrefsContext);
