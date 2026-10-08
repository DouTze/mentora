import type { Key } from "./index";

// Forms that differ from en.ts. Keep these in sync when editing or retranslating
// the corresponding messages. Without a numeric variable, the base text is used.
export const enPlurals: Partial<Record<Key, { variable: string; one?: string; other?: string }>> = {
  "material.unit.page": { variable: "count", other: "pages" },
  "material.unit.line": { variable: "count", other: "lines" },
  "reader.study.parts": { variable: "count", one: "{count} part ready" },
  "shelf.pages": { variable: "count", one: "{count} page" },
  "shelf.parts": { variable: "count", one: "{count} chapter" },
  "shelf.bar.intoCount": { variable: "count", one: "Add {count} book to" },
  "home.more.books": { variable: "count", one: "Scroll down to see {count} book picked for you" },
  "history.reading.title": { variable: "total", one: "Guided reading · {total} section" },
  "reading.meta.segModel": { variable: "total", one: "The model organized the text into {total} section by topic" },
  "reading.meta.segParagraph": { variable: "total", one: "The text was organized into {total} section by paragraph" },
  "reading.skipped.summary": { variable: "n", one: "Skipped {n} section that doesn’t need guided reading (expand to view)" },
  "reading.unverified.summary": { variable: "n", one: "We couldn’t verify {n} point in this reading guide automatically. Compare it with the source text." },
  "reading.finish.confirmText": { variable: "count", one: "Skip the remaining {count} section and practice what you’ve read so far?" },
  "reading.previous.show": { variable: "count", one: "Review the previous {count} section" },
};
