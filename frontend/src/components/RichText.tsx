// Renders the model's text: a small Markdown subset (paragraphs, bullets, numbered lists, tables, headings,
// blockquotes, **bold**, *italic*, `code`) plus KaTeX math ($…$ inline, $$…$$ display). Everything is built
// as React elements — only KaTeX's own output goes in as HTML — so model text can never inject markup.
import katex from "katex";
import { Fragment, type ReactNode } from "react";

interface Props {
  text: string | null | undefined;
  /** render inline only (no paragraph wrappers), e.g. inside a <strong> or a button label */
  inline?: boolean;
  className?: string;
}

export function RichText({ text, inline = false, className }: Props) {
  if (!text) return null;
  if (inline) return <>{renderInline(text)}</>;
  return <div className={className}>{renderBlocks(text)}</div>;
}

// textgreek's \textgamma …: models write them for Greek letters (also inside \text{…}), KaTeX does not know them and
// showed them in red. The letter itself reads in text and in math alike.
const GREEK: [string, string, string][] = [["alpha", "α", "Α"], ["beta", "β", "Β"], ["gamma", "γ", "Γ"], ["delta", "δ", "Δ"],
  ["epsilon", "ε", "Ε"], ["zeta", "ζ", "Ζ"], ["eta", "η", "Η"], ["theta", "θ", "Θ"], ["iota", "ι", "Ι"], ["kappa", "κ", "Κ"],
  ["lambda", "λ", "Λ"], ["mu", "μ", "Μ"], ["nu", "ν", "Ν"], ["xi", "ξ", "Ξ"], ["omicron", "ο", "Ο"], ["pi", "π", "Π"],
  ["rho", "ρ", "Ρ"], ["sigma", "σ", "Σ"], ["tau", "τ", "Τ"], ["upsilon", "υ", "Υ"], ["phi", "φ", "Φ"], ["chi", "χ", "Χ"],
  ["psi", "ψ", "Ψ"], ["omega", "ω", "Ω"]];
const MACROS: Record<string, string> = Object.fromEntries(
  GREEK.flatMap(([g, lo, up]) => [[`\\text${g}`, lo], [`\\text${g[0].toUpperCase()}${g.slice(1)}`, up]]),
);
const KATEX = { throwOnError: false, strict: "ignore", output: "htmlAndMathml", macros: MACROS } as const;

function math(src: string, display: boolean): ReactNode {
  let html: string;
  try {
    html = katex.renderToString(src, { ...KATEX, macros: { ...MACROS }, displayMode: display });
  } catch {
    return <code>{src}</code>;
  }
  return display ? <div className="math-display" dangerouslySetInnerHTML={{ __html: html }} /> : <span dangerouslySetInnerHTML={{ __html: html }} />;
}

/** A display formula written inside a line of text ("… given by $$…$$ where …", as PDF text is imported): its own
 * line, set as a display, without leaving the paragraph. */
function mathBlockInLine(src: string): ReactNode {
  let html: string;
  try {
    html = katex.renderToString(src, { ...KATEX, macros: { ...MACROS }, displayMode: true });
  } catch {
    return <code>{src}</code>;
  }
  return <span className="math-display math-in-line" dangerouslySetInnerHTML={{ __html: html }} />;
}

// -- inline -----------------------------------------------------------------
// tokens, first match wins: display math within a line, inline math, code, bold, italic
const INLINE_RE = /(\$\$(?:\\.|[^$\\])+?\$\$)|(\$(?!\s)(?:\\.|[^$\\\n])+?(?<!\s)\$)|(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\*(?!\s)[^*\n]+?(?<!\s)\*)/g;

export function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    const i = m.index ?? 0;
    if (i > last) out.push(text.slice(last, i));
    const [tok, display, dollar, code, bold, italic] = m;
    if (display) out.push(<Fragment key={k++}>{mathBlockInLine(tok.slice(2, -2))}</Fragment>);
    else if (dollar) out.push(<Fragment key={k++}>{math(tok.slice(1, -1), false)}</Fragment>);
    else if (code) out.push(<code key={k++}>{tok.slice(1, -1)}</code>);
    else if (bold) out.push(<strong key={k++}>{renderInline(tok.slice(2, -2))}</strong>);
    else if (italic) out.push(<em key={k++}>{renderInline(tok.slice(1, -1))}</em>);
    last = i + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// -- blocks -----------------------------------------------------------------
type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "h"; level: number; text: string }
  | { kind: "quote"; lines: string[] }
  | { kind: "math"; src: string }
  | { kind: "table"; rows: string[][] };

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  const flushP = (buf: string[]) => buf.length && blocks.push({ kind: "p", lines: buf });
  let p: string[] = [];
  while (i < lines.length) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) {
      flushP(p);
      p = [];
      i++;
      continue;
    }
    // display math: a line that is exactly $$, or starts with $$ (single-line $$…$$ too)
    if (line.startsWith("$$")) {
      flushP(p);
      p = [];
      const single = line.length > 2 && line.endsWith("$$") && line.length > 4;
      if (single) {
        blocks.push({ kind: "math", src: line.slice(2, -2) });
        i++;
        continue;
      }
      const buf: string[] = [];
      let rest = line.slice(2);
      i++;
      while (i < lines.length && !lines[i].trim().endsWith("$$") && lines[i].trim() !== "$$") buf.push(lines[i++]);
      if (i < lines.length) {
        const end = lines[i].trim();
        if (end !== "$$") buf.push(end.slice(0, -2));
        i++;
      }
      blocks.push({ kind: "math", src: [rest, ...buf].join("\n") });
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flushP(p);
      p = [];
      blocks.push({ kind: "h", level: h[1].length, text: h[2] });
      i++;
      continue;
    }
    if (/^[-*•]\s+/.test(line) || /^\d+[.)]\s+/.test(line)) {
      flushP(p);
      p = [];
      const ordered = /^\d+[.)]\s+/.test(line);
      const items: string[] = [];
      while (i < lines.length) {
        const l = lines[i].trim();
        const m = ordered ? /^\d+[.)]\s+(.*)$/.exec(l) : /^[-*•]\s+(.*)$/.exec(l);
        if (m) items.push(m[1]);
        else if (l && /^\s{2,}/.test(lines[i]) && items.length) items[items.length - 1] += " " + l; // continuation line
        else break;
        i++;
      }
      blocks.push({ kind: ordered ? "ol" : "ul", items });
      continue;
    }
    if (line.startsWith(">")) {
      flushP(p);
      p = [];
      const buf: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) buf.push(lines[i++].trim().replace(/^>\s?/, ""));
      blocks.push({ kind: "quote", lines: buf });
      continue;
    }
    if (line.startsWith("|") && i + 1 < lines.length && /^\|?\s*:?-{2,}/.test(lines[i + 1].trim())) {
      flushP(p);
      p = [];
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const l = lines[i].trim();
        if (!/^\|?\s*:?-{2,}/.test(l)) rows.push(l.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim()));
        i++;
      }
      blocks.push({ kind: "table", rows });
      continue;
    }
    p.push(line);
    i++;
  }
  flushP(p);
  return blocks;
}

function renderBlocks(text: string): ReactNode[] {
  return parseBlocks(text).map((b, k) => {
    switch (b.kind) {
      case "p":
        return <p key={k}>{renderInline(b.lines.join("\n"))}</p>;
      case "h": {
        const Tag = (`h${Math.min(6, b.level + 2)}` as unknown) as "h3";
        return <Tag key={k}>{renderInline(b.text)}</Tag>;
      }
      case "ul":
        return (
          <ul key={k}>
            {b.items.map((it, j) => (
              <li key={j}>{renderInline(it)}</li>
            ))}
          </ul>
        );
      case "ol":
        return (
          <ol key={k}>
            {b.items.map((it, j) => (
              <li key={j}>{renderInline(it)}</li>
            ))}
          </ol>
        );
      case "quote":
        return <blockquote key={k}>{renderBlocks(b.lines.join("\n"))}</blockquote>;
      case "math":
        return <Fragment key={k}>{math(b.src, true)}</Fragment>;
      case "table": {
        const [head, ...body] = b.rows;
        return (
          <div className="table-wrap" key={k}>
            <table>
              <thead>
                <tr>{head.map((c, j) => <th key={j}>{renderInline(c)}</th>)}</tr>
              </thead>
              <tbody>
                {body.map((r, ri) => (
                  <tr key={ri}>{r.map((c, j) => <td key={j}>{renderInline(c)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }
    }
  });
}
