// 接續閱讀: the readings left unfinished, one per book. The latest is laid out like a book on the desk — cover, title,
// where it was left and how far along — with one clear way back in; the others follow as quiet rows.
import { useState } from "react";
import { api } from "../api";
import type { Library, SessionListItem } from "../types";
import { ClothCover, KIND_LABEL } from "./Bookshelf";
import { Icon } from "./Icons";
import { useLocale, useT } from "../i18n";

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [["day", 86400], ["hour", 3600], ["minute", 60]];

/** "3 hours ago" / "3 小時前"; under a minute reads as "now" */
function ago(iso: string, locale: string): string {
  const secs = (Date.parse(iso) - Date.now()) / 1000;
  const fmt = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [unit, size] of UNITS) if (Math.abs(secs) >= size) return fmt.format(Math.round(secs / size), unit);
  return fmt.format(0, "second");
}

function Cover({ session, library }: { session: SessionListItem; library: Library }) {
  const [failed, setFailed] = useState(false);
  const material = session.source ? library.materials.find((m) => m.id === session.source!.material_id) : undefined;
  const title = session.source?.material_title ?? session.content_title;
  if (material?.kind === "pdf" && !failed) {
    return <img src={api.materialCoverUrl(material.id)} alt="" draggable={false} onError={() => setFailed(true)} />;
  }
  return <ClothCover id={material?.id ?? session.content_title} title={title} label={material ? KIND_LABEL[material.kind] : "DEMO"} foot="" />;
}

function Progress({ session }: { session: SessionListItem }) {
  const t = useT();
  const p = session.progress;
  const share = p.stage === "quiz" || !p.total ? 1 : p.read / p.total;
  return (
    <span className="resume-progress">
      <span className="resume-bar" aria-hidden="true">
        <span style={{ width: `${Math.max(share * 100, 3)}%` }} />
      </span>
      {/* `read` passages are done; the one being read is the next */}
      <span className="resume-step">{p.stage === "quiz" ? t("resume.quiz") : t("resume.reading", { current: Math.min(p.read + 1, p.total), total: p.total })}</span>
    </span>
  );
}

export function ResumeReading({ sessions, library, onOpen }: { sessions: SessionListItem[]; library: Library; onOpen: (id: string) => void }) {
  const t = useT();
  const locale = useLocale();
  const [first, ...rest] = sessions;
  if (!first) return null;
  const title = (s: SessionListItem) => s.source?.material_title ?? s.content_title;
  const where = (s: SessionListItem) => [s.source?.label, ago(s.updated_at, locale)].filter(Boolean).join(" · ");
  return (
    <section className="home-resume">
      <h2 className="home-label">{t("home.active")}</h2>
      <button type="button" className="resume-feature" onClick={() => onOpen(first.id)}>
        <span className="resume-cover large">
          <Cover session={first} library={library} />
        </span>
        <span className="resume-body">
          <span className="resume-title">{title(first)}</span>
          <span className="resume-meta">{where(first)}</span>
          <Progress session={first} />
        </span>
        <span className="resume-go">
          {t("resume.continue")}
          <Icon.chevron />
        </span>
      </button>
      {rest.length > 0 && (
        <ul className="resume-rest">
          {rest.map((s) => (
            <li key={s.id}>
              <button type="button" className="resume-row" onClick={() => onOpen(s.id)}>
                <span className="resume-cover">
                  <Cover session={s} library={library} />
                </span>
                <span className="resume-body">
                  <span className="resume-title">{title(s)}</span>
                  <span className="resume-meta">{where(s)}</span>
                </span>
                <Progress session={s} />
                <Icon.chevron />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
