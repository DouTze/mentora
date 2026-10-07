// The home page's bookshelf: books the scout guessed, standing on one shelf line like a bookcase. Every cover keeps
// the same height and its own proportion, so a slim paperback, an A4 handout and a drawn cover for a text file sit
// together on one baseline. A PDF shows its first page; text files (and cloud files that must not be downloaded
// for a picture) get a cloth-bound cover drawn from a small, muted palette.
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ShelfBook } from "../types";
import { Icon } from "./Icons";
import { useT } from "../i18n";

const COVER_H = 168;
// ink on cloth; deep, quiet tones that sit well next to real covers and each other
const CLOTH = [
  ["#2f3e46", "#e7e1d3"],
  ["#5b2c2a", "#f0e3d0"],
  ["#23405a", "#e4ebf1"],
  ["#3c4a3a", "#e9e8dc"],
  ["#6a4c2c", "#f3e7d3"],
  ["#463a52", "#ece4ef"],
  ["#1f2a2e", "#d9cfb8"],
];

function hash(s: string): number {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0;
  return Math.abs(h);
}

const KIND_LABEL: Record<ShelfBook["kind"], string> = { pdf: "PDF", md: "MARKDOWN", txt: "TEXT" };

function DrawnCover({ book }: { book: ShelfBook }) {
  const h = hash(book.id);
  const [bg, ink] = CLOTH[h % CLOTH.length];
  return (
    <div className={`cover-drawn v${h % 2}`} style={{ background: bg, color: ink }}>
      <span className="cover-kind">{KIND_LABEL[book.kind]}</span>
      <span className="cover-title">{book.title}</span>
      <span className="cover-rule" />
      <span className="cover-foot">{book.author || book.folder}</span>
    </div>
  );
}

function Book({ book, index, selected, onToggle }: { book: ShelfBook; index: number; selected: boolean; onToggle: () => void }) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  const aspect = book.kind === "pdf" && !book.cloud && !failed ? Math.min(Math.max(book.aspect, 0.62), 0.8) : 0.68;
  const width = Math.round(COVER_H * aspect);
  const meta = book.reason || (book.pages ? t("shelf.pages", { count: book.pages }) : book.parts ? t("shelf.parts", { count: book.parts }) : book.folder);
  return (
    <li className="book-slot" style={{ width, animationDelay: `${index * 60}ms` }}>
      <button
        type="button"
        className={`book ${selected ? "selected" : ""}`}
        style={{ height: COVER_H }}
        aria-pressed={selected}
        title={book.reason ? `${book.title}\n${book.reason}` : book.title}
        onClick={onToggle}
      >
        {book.kind === "pdf" && !book.cloud && !failed ? (
          <img src={api.shelfCoverUrl(book.id)} alt="" loading="lazy" draggable={false} onError={() => setFailed(true)} />
        ) : (
          <DrawnCover book={book} />
        )}
        <span className="book-check" aria-hidden="true">
          <Icon.check />
        </span>
      </button>
      <span className="book-title">{book.title}</span>
      <span className="book-meta">{meta}</span>
    </li>
  );
}

/** Ghost books while the first shelf is being prepared: varied widths, a slow staggered breath. */
const GHOSTS = [0.66, 0.74, 0.62, 0.7, 0.78, 0.64];

export function Bookshelf({ books, loading, selected, onToggle }: { books: ShelfBook[]; loading: boolean; selected: Set<string>; onToggle: (id: string) => void }) {
  // the shelf waits below the first screen; its books come in one by one when it scrolls into view
  const ref = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    if (typeof IntersectionObserver === "undefined") return setSeen(true);
    const io = new IntersectionObserver(([e]) => e.isIntersecting && setSeen(true), { threshold: 0.25 });
    io.observe(el);
    return () => io.disconnect();
  }, [seen]);
  return (
    <div ref={ref} className={`shelf ${seen ? "seen" : ""}`}>
      <ul className="shelf-row">
        {books.length === 0 && loading
          ? GHOSTS.map((a, i) => (
              <li key={i} className="book-slot ghost" style={{ width: Math.round(COVER_H * a), animationDelay: `${i * 140}ms` }}>
                <span className="book" style={{ height: COVER_H }} />
              </li>
            ))
          : books.map((b, i) => <Book key={b.id} book={b} index={i} selected={selected.has(b.id)} onToggle={() => onToggle(b.id)} />)}
      </ul>
    </div>
  );
}
