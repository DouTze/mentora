import { memo, useMemo } from "react";
import { desktop } from "../desktop";
import { useT } from "../i18n";
import type { PdfLink, PdfPageLayer, PdfRect } from "../types";
import { Icon } from "./Icons";

/**
 * What lies over a PDF page image, as PrintCraft draws it: the page's text as invisible runs at the glyphs' places
 * (select and copy in reading order), links to follow, and the page's images to save. The backend
 * (app/pdfdoc/layer.py) gives every box in view space (points, y down).
 *
 * Zooming must not touch this layer: places are percentages of the page, and a run's font size is
 * `calc(var(--k) * var(--fs))`: `--fs` is its em in points, `--k` the page's (`.paper`) screen pixels per point. A zoom
 * changes that one variable and the runs are not rendered again (pdf.js does the same with its --scale-factor).
 */

// a web address a PDF may point to; anything else (javascript:, file:) is not followed
const SAFE_URI = /^(https?:|mailto:|ftp:)/i;

let measureCtx: CanvasRenderingContext2D | null | undefined;
/** Width of `text` at a font size of 1 in the layer's face, or null where the browser cannot measure (tests). */
function unitWidth(text: string): number | null {
  if (measureCtx === undefined) {
    try {
      measureCtx = document.createElement("canvas").getContext("2d");
      if (measureCtx) measureCtx.font = "100px sans-serif";
    } catch {
      measureCtx = null;
    }
  }
  const w = measureCtx?.measureText(text).width;
  return w ? w / 100 : null;
}

type Run = { t: string; sp?: boolean; style: React.CSSProperties };

/** A run's place: where it starts (percent of the page), its font size, and the turn and stretch that fit its box. */
function place(t: string, [x0, y0, x1, y1]: PdfRect, quarter: number, [pw, ph]: [number, number]): React.CSSProperties {
  const across = quarter % 2 ? x1 - x0 : y1 - y0; // the em: the box's size across the line
  const along = quarter % 2 ? y1 - y0 : x1 - x0;
  const w = unitWidth(t);
  const sx = w ? along / (w * across) : 1;
  const [left, top, turn] = quarter === 1 ? [x1, y0, 90] : quarter === 2 ? [x1, y1, 180] : quarter === 3 ? [x0, y1, 270] : [x0, y0, 0];
  return {
    left: pct(left, pw),
    top: pct(top, ph),
    ["--fs" as string]: across.toFixed(2), // the em in points; .pdf-run sizes the font from it and the page's --k
    transform: `${turn ? `rotate(${turn}deg) ` : ""}scaleX(${sx.toFixed(4)})`,
  };
}

const pct = (v: number, of: number) => `${((v / of) * 100).toFixed(3)}%`;

function linkTitle(link: PdfLink, t: ReturnType<typeof useT>): string {
  if (link.kind === "page") return t("reader.link.page", { n: link.page });
  if (link.kind === "uri") return link.uri;
  return t("reader.link.unsupported");
}

const boxOf = ([x0, y0, x1, y1]: PdfRect, [w, h]: [number, number]) => ({ left: pct(x0, w), top: pct(y0, h), width: pct(x1 - x0, w), height: pct(y1 - y0, h) });

/** Pass stable `imageUrl` / `onGoto`: the layer renders again only when they or the page's layer change. */
export const PdfLayer = memo(function PdfLayer({ layer, imageUrl, onGoto }: {
  layer: PdfPageLayer;
  imageUrl: (xref: number) => string;
  /** follow a link inside the document: page 1-based, y in that page's view space */
  onGoto: (page: number, y?: number) => void;
}) {
  const t = useT();
  // measured once per page: the stretch of each run does not change with the zoom
  const lines = useMemo(
    () => layer.lines.map((ln) => ln.runs.map((r): Run => ({ t: r.t, sp: r.sp, style: place(r.t, r.rect, layer.quarter, layer.size) }))),
    [layer],
  );
  const box = (r: PdfRect) => boxOf(r, layer.size);
  const notes = layer.annotations.filter((a) => a.content && !["Highlight", "Underline", "StrikeOut", "Squiggly"].includes(a.type));
  return (
    <div className="pdf-layer">
      {layer.images.map((im, i) =>
        // a figure, not a bullet or a rule: at least a tenth of the page each way
        im.xref > 0 && im.rect[2] - im.rect[0] > layer.size[0] / 10 && im.rect[3] - im.rect[1] > layer.size[1] / 10 ? (
          <div key={`i${i}`} className="pdf-image" style={box(im.rect)}>
            <a href={imageUrl(im.xref)} download={`page-${layer.page}-image-${i + 1}`} className="pdf-image-save" title={t("reader.image.save")} aria-label={t("reader.image.save")}>
              <Icon.download />
            </a>
          </div>
        ) : null,
      )}
      <div className="pdf-text" aria-label={t("reader.pageText", { n: layer.label })}>
        {lines.map((runs, li) => (
          <span key={li} className="pdf-line">
            {runs.map((r, ri) => (
              <span key={ri}>
                {r.sp ? " " : ""}
                <span className="pdf-run" style={r.style}>
                  {r.t}
                </span>
              </span>
            ))}
            {"\n"}
          </span>
        ))}
      </div>
      {notes.map((a) => (
        <div key={`n${a.index}`} className="pdf-note" style={box(a.rect)} title={a.author ? `${a.author}：${a.content}` : (a.content ?? "")} />
      ))}
      {layer.links.map((ln, i) => {
        const title = linkTitle(ln, t);
        if (ln.kind === "page")
          return (
            <a
              key={`l${i}`}
              className="pdf-link"
              href={`#page-${ln.page}`}
              style={box(ln.rect)}
              title={title}
              aria-label={title}
              onClick={(e) => {
                e.preventDefault();
                onGoto(ln.page, ln.y);
              }}
            />
          );
        if (ln.kind === "uri" && SAFE_URI.test(ln.uri))
          // the desktop app opens web addresses in the system browser (src-tauri/src/lib.rs, on_navigation)
          return <a key={`l${i}`} className="pdf-link" href={ln.uri} target={desktop ? undefined : "_blank"} rel="noopener noreferrer" style={box(ln.rect)} title={title} aria-label={title} />;
        return <span key={`l${i}`} className="pdf-link off" style={box(ln.rect)} title={title} />;
      })}
    </div>
  );
});
