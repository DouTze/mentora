// Inline 16px stroke icons (no icon package: the app has to work offline).
import type { ReactNode } from "react";

function Svg({ children }: { children: ReactNode }) {
  return (
    <svg className="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

export const Icon = {
  /** sidebar panel; the arrow points the way the panel will move */
  collapse: () => (
    <Svg>
      <rect x="1.5" y="2.5" width="13" height="11" rx="2" />
      <path d="M5.5 2.5v11M11 6.2 9.2 8l1.8 1.8" />
    </Svg>
  ),
  expand: () => (
    <Svg>
      <rect x="1.5" y="2.5" width="13" height="11" rx="2" />
      <path d="M5.5 2.5v11M9.2 6.2 11 8 9.2 9.8" />
    </Svg>
  ),
  chevron: () => (
    <Svg>
      <path d="M6 4l4 4-4 4" />
    </Svg>
  ),
  home: () => (
    <Svg>
      <path d="M2.5 7.5 8 2.5l5.5 5v6h-4v-4h-3v4h-4z" />
    </Svg>
  ),
  learn: () => (
    <Svg>
      <path d="M1.5 6 8 3l6.5 3L8 9z" />
      <path d="M4 7.3v3.2c1 1 2.4 1.5 4 1.5s3-.5 4-1.5V7.3" />
    </Svg>
  ),
  history: () => (
    <Svg>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 5v3.2l2 1.3" />
    </Svg>
  ),
  sliders: () => (
    <Svg>
      <path d="M2 4.5h7M12 4.5h2M2 11.5h2M7 11.5h7" />
      <circle cx="10.5" cy="4.5" r="1.5" />
      <circle cx="5.5" cy="11.5" r="1.5" />
    </Svg>
  ),
  gear: () => (
    <Svg>
      <path d="M6.9 1.6h2.2l.4 1.7 1.3.6 1.5-.9 1.6 1.6-.9 1.5.6 1.3 1.7.4v2.2l-1.7.4-.6 1.3.9 1.5-1.6 1.6-1.5-.9-1.3.6-.4 1.7H6.9l-.4-1.7-1.3-.6-1.5.9-1.6-1.6.9-1.5-.6-1.3-1.7-.4V6.9l1.7-.4.6-1.3-.9-1.5 1.6-1.6 1.5.9 1.3-.6z" />
      <circle cx="8" cy="8" r="2.1" />
    </Svg>
  ),
  /** 伴讀: an open book with a small companion star */
  companion: () => (
    <Svg>
      <path d="M8 4.5C6.6 3.4 4.6 3 2 3.2v9c2.6-.2 4.6.2 6 1.3 1.4-1.1 3.4-1.5 6-1.3v-9c-2.6-.2-4.6.2-6 1.3zM8 4.5v9" />
    </Svg>
  ),
  close: () => (
    <Svg>
      <path d="M4 4l8 8M12 4l-8 8" />
    </Svg>
  ),
  folderPlus: () => (
    <Svg>
      <path d="M1.5 4.5a1 1 0 0 1 1-1h3l1.5 1.5h6.5a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z" />
      <path d="M8 7.2v3.6M6.2 9h3.6" />
    </Svg>
  ),
  upload: () => (
    <Svg>
      <path d="M8 10.5v-8M5 5.2l3-3 3 3M2.5 10v2.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V10" />
    </Svg>
  ),
  plus: () => (
    <Svg>
      <path d="M8 3.5v9M3.5 8h9" />
    </Svg>
  ),
  more: () => (
    <Svg>
      <circle cx="3.5" cy="8" r=".6" fill="currentColor" />
      <circle cx="8" cy="8" r=".6" fill="currentColor" />
      <circle cx="12.5" cy="8" r=".6" fill="currentColor" />
    </Svg>
  ),
  trash: () => (
    <Svg>
      <path d="M2.5 4.5h11M6 4.5v-2h4v2M4 4.5l.7 9h6.6l.7-9" />
    </Svg>
  ),
};

/** File-type chip used in the tree and on material pages. */
export function KindTag({ kind }: { kind: string }) {
  return <span className={`kind kind-${kind}`}>{kind.toUpperCase()}</span>;
}
