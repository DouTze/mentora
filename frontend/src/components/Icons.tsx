// Inline line icons (no icon package: the app has to work offline). Drawn on a 24 grid and shown at 16px, so the
// 1.5 stroke lands as a fine ~1px hairline; one stroke weight, round ends and quiet geometry across the whole set.
import type { ReactNode } from "react";

function Svg({ children }: { children: ReactNode }) {
  return (
    <svg className="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

export const Icon = {
  /** sidebar panel; the arrow points the way the panel will move */
  collapse: () => (
    <Svg>
      <rect x="3" y="4.5" width="18" height="15" rx="3" />
      <path d="M9 4.5v15M15.5 10 13.5 12l2 2" />
    </Svg>
  ),
  expand: () => (
    <Svg>
      <rect x="3" y="4.5" width="18" height="15" rx="3" />
      <path d="M9 4.5v15M13.5 10l2 2-2 2" />
    </Svg>
  ),
  chevron: () => (
    <Svg>
      <path d="M9.5 6.5 15 12l-5.5 5.5" />
    </Svg>
  ),
  /** a gabled house with an arched door */
  home: () => (
    <Svg>
      <path d="M3.5 10.5 12 4l8.5 6.5M6 9v10.5h12V9" />
      <path d="M10 19.5V16a2 2 0 0 1 4 0v3.5" />
    </Svg>
  ),
  /** mortarboard with a tassel */
  learn: () => (
    <Svg>
      <path d="M2.5 9.5 12 5l9.5 4.5L12 14z" />
      <path d="M6.5 11.6V16c1.4 1.3 3.3 2 5.5 2s4.1-.7 5.5-2v-4.4M21.5 9.5v4.5" />
      <circle cx="21.5" cy="15.6" r=".9" fill="currentColor" stroke="none" />
    </Svg>
  ),
  /** hourglass, the last grains settled at the bottom */
  history: () => (
    <Svg>
      <path d="M6.5 3.5h11M6.5 20.5h11" />
      <path d="M8 3.5v2.2c0 2.4 4 3.8 4 6.3s-4 3.9-4 6.3v2.2M16 3.5v2.2c0 2.4-4 3.8-4 6.3s4 3.9 4 6.3v2.2" />
      <path d="M9 19.6c.8-1.3 1.8-2 3-2s2.2.7 3 2z" fill="currentColor" stroke="none" />
    </Svg>
  ),
  sliders: () => (
    <Svg>
      <path d="M4 7h3M11 7h9M4 12h9M17 12h3M4 17h5M13 17h7" />
      <circle cx="9" cy="7" r="2" />
      <circle cx="15" cy="12" r="2" />
      <circle cx="11" cy="17" r="2" />
    </Svg>
  ),
  gear: () => (
    <Svg>
      <path d="M10.23 4.92L10.69 2.69A9.4 9.4 0 0 1 13.31 2.69L13.77 4.92A7.3 7.3 0 0 1 15.76 5.74L17.66 4.49A9.4 9.4 0 0 1 19.51 6.34L18.26 8.24A7.3 7.3 0 0 1 19.08 10.23L21.31 10.69A9.4 9.4 0 0 1 21.31 13.31L19.08 13.77A7.3 7.3 0 0 1 18.26 15.76L19.51 17.66A9.4 9.4 0 0 1 17.66 19.51L15.76 18.26A7.3 7.3 0 0 1 13.77 19.08L13.31 21.31A9.4 9.4 0 0 1 10.69 21.31L10.23 19.08A7.3 7.3 0 0 1 8.24 18.26L6.34 19.51A9.4 9.4 0 0 1 4.49 17.66L5.74 15.76A7.3 7.3 0 0 1 4.92 13.77L2.69 13.31A9.4 9.4 0 0 1 2.69 10.69L4.92 10.23A7.3 7.3 0 0 1 5.74 8.24L4.49 6.34A9.4 9.4 0 0 1 6.34 4.49L8.24 5.74A7.3 7.3 0 0 1 10.23 4.92Z" />
      <circle cx="12" cy="12" r="3" />
    </Svg>
  ),
  /** 伴讀: the owl companion, drawn in line */
  companion: () => (
    <Svg>
      <path d="M6.2 9.6 5.2 4.5l4 2.2a7.6 7.6 0 0 1 5.6 0l4-2.2-1 5.1" />
      <path d="M6.2 9.6C5.4 11 5 12.5 5 14c0 3.9 3.1 6.5 7 6.5s7-2.6 7-6.5c0-1.5-.4-3-1.2-4.4" />
      <circle cx="9.4" cy="11.6" r="1.8" />
      <circle cx="14.6" cy="11.6" r="1.8" />
      <path d="m11.2 14.6.8 1 .8-1" />
    </Svg>
  ),
  /** 書櫃: spines on a shelf, the last one leaning */
  shelf: () => (
    <Svg>
      <path d="M3.5 20h17M6 19.5V6.5h3v13M10 19.5V9h3v10.5" />
      <path d="m15 19.4 3-11.1 2.6.7-3 11.1" />
    </Svg>
  ),
  /** 導讀: an open book, a ribbon falling from the spine */
  book: () => (
    <Svg>
      <path d="M12 6.8C10.3 5.6 8 5 4.5 5v12.5c3.5 0 5.8.6 7.5 1.8 1.7-1.2 4-1.8 7.5-1.8V5C16 5 13.7 5.6 12 6.8z" />
      <path d="M12 6.8v12.5" />
    </Svg>
  ),
  /** 出題: a question mark inside a card */
  quiz: () => (
    <Svg>
      <rect x="4.5" y="3.5" width="15" height="17" rx="2.5" />
      <path d="M9.8 9.6a2.3 2.3 0 1 1 3.3 2.1c-.7.3-1.1.9-1.1 1.6v.4" />
      <circle cx="12" cy="16.6" r=".9" fill="currentColor" stroke="none" />
    </Svg>
  ),
  close: () => (
    <Svg>
      <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
    </Svg>
  ),
  folderPlus: () => (
    <Svg>
      <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2h9A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z" />
      <path d="M12 11v5M9.5 13.5h5" />
    </Svg>
  ),
  upload: () => (
    <Svg>
      <path d="M12 15V4.5M8 8.5l4-4 4 4" />
      <path d="M4 14.5v3A2.5 2.5 0 0 0 6.5 20h11a2.5 2.5 0 0 0 2.5-2.5v-3" />
    </Svg>
  ),
  attach: () => (
    <Svg>
      <path d="m19.5 11.6-7.4 7.4a4.8 4.8 0 0 1-6.8-6.8l7.9-7.9a3.2 3.2 0 0 1 4.5 4.5l-7.8 7.8a1.6 1.6 0 0 1-2.3-2.3l7.1-7.1" />
    </Svg>
  ),
  arrowUp: () => (
    <Svg>
      <path d="M12 18.5V6M6.5 11.5 12 6l5.5 5.5" />
    </Svg>
  ),
  check: () => (
    <Svg>
      <path d="m5.5 12.5 4.2 4.2 8.8-9.4" />
    </Svg>
  ),
  plus: () => (
    <Svg>
      <path d="M12 5.5v13M5.5 12h13" />
    </Svg>
  ),
  more: () => (
    <Svg>
      <circle cx="6" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="18" cy="12" r="1.1" fill="currentColor" stroke="none" />
    </Svg>
  ),
  toc: () => (
    <Svg>
      <path d="M9 6.5h10.5M9 12h10.5M9 17.5h10.5" />
      <circle cx="5" cy="6.5" r="1" fill="currentColor" stroke="none" />
      <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="5" cy="17.5" r="1" fill="currentColor" stroke="none" />
    </Svg>
  ),
  download: () => (
    <Svg>
      <path d="M12 4.5v10.5M7.5 11l4.5 4.5 4.5-4.5M5 19.5h14" />
    </Svg>
  ),
  trash: () => (
    <Svg>
      <path d="M4 6.5h16M9.5 6.5v-2h5v2" />
      <path d="m6 6.5.9 12.1A1.5 1.5 0 0 0 8.4 20h7.2a1.5 1.5 0 0 0 1.5-1.4L18 6.5M10 10.5v6M14 10.5v6" />
    </Svg>
  ),
};

/** File-type chip used in the tree and on material pages. */
export function KindTag({ kind }: { kind: string }) {
  return <span className={`kind kind-${kind}`}>{kind.toUpperCase()}</span>;
}
