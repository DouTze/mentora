// Source dictionary. Keys are grouped by the screen they belong to; en.ts is translated from this file
// (bun run i18n:translate asks codex to fill in whatever keys en.ts is missing).
// `{name}` placeholders are filled in by t(key, { name }).
import { shell } from "./zh-Hant.shell";
import { pages } from "./zh-Hant.pages";

export const zh = { ...shell, ...pages };
