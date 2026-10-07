// UI strings in 繁體中文 and English. zh-Hant is the source of truth; a key missing from en falls back to it.
import { createContext, useContext } from "react";
import { zh } from "./zh-Hant";
import { en } from "./en";
import { enPlurals } from "./en.plurals";

export type Locale = "zh-Hant" | "en";
export type Key = keyof typeof zh;
export type Dict = Partial<Record<Key, string>>;
export type Vars = Record<string, string | number>;
export type T = (key: Key, vars?: Vars) => string;

export const LOCALES: { id: Locale; label: string }[] = [
  { id: "zh-Hant", label: "繁體中文" },
  { id: "en", label: "English" },
];

const DICTS: Record<Locale, Dict> = { "zh-Hant": zh, en };
const englishPlurals = new Intl.PluralRules("en");

export function translate(locale: Locale, key: Key, vars?: Vars): string {
  let text = DICTS[locale][key] ?? zh[key] ?? key;
  const forms = locale === "en" ? enPlurals[key] : undefined;
  const count = forms && vars?.[forms.variable];
  if (forms && typeof count === "number") {
    text = (englishPlurals.select(count) === "one" ? forms.one : forms.other) ?? text;
  }
  return vars ? text.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m)) : text;
}

export const I18nContext = createContext<{ locale: Locale; t: T }>({ locale: "zh-Hant", t: (k, v) => translate("zh-Hant", k, v) });

export const useT = () => useContext(I18nContext).t;
export const useLocale = () => useContext(I18nContext).locale;
