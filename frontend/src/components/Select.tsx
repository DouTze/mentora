// The app's own drop-downs, drawn like the rest of it instead of by the operating system.
//
//   <Select>      pick one value from a list (replaces <select>)
//   <ComboInput>  free text with suggestions (replaces <input list> + <datalist>)
//
// Both keep the focus on the control and point at the highlighted option with aria-activedescendant, so the
// keyboard works as with the native ones: ↑↓ move, Enter / Space choose, Esc closes, Tab leaves.
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Icon } from "./Icons";

export interface Option<V extends string = string> {
  value: V;
  label: string;
}

/** Open the list upwards when there is not room for it below. */
function useFlip(anchor: React.RefObject<HTMLElement | null>, open: boolean, rows: number): boolean {
  const [up, setUp] = useState(false);
  useEffect(() => {
    if (!open || !anchor.current) return;
    const r = anchor.current.getBoundingClientRect();
    const need = Math.min(rows, 8) * 34 + 12;
    setUp(window.innerHeight - r.bottom < need && r.top > window.innerHeight - r.bottom);
  }, [open, rows]);
  return up;
}

/** Close when a press lands outside `box`. */
function useOutside(box: React.RefObject<HTMLElement | null>, open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !box.current?.contains(e.target as Node) && close();
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
}

export function Select<V extends string>({ value, options, onChange, label, className = "", disabled }: {
  value: V;
  options: Option<V>[];
  onChange: (v: V) => void;
  /** accessible name; a visible <label> around the control works as well */
  label?: string;
  className?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const box = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  const up = useFlip(box, open, options.length);
  useOutside(box, open, () => setOpen(false));
  const current = options.find((o) => o.value === value) ?? options[0];

  const show = () => {
    setHi(Math.max(0, options.findIndex((o) => o.value === value)));
    setOpen(true);
  };
  const choose = (i: number) => {
    setOpen(false);
    if (options[i] && options[i].value !== value) onChange(options[i].value);
  };
  const onKey = (e: KeyboardEvent) => {
    if (!open) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
        e.preventDefault();
        show();
      }
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setHi((i) => (i + (e.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      choose(hi);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();   // the settings dialog closes on Escape too; only the list should
      setOpen(false);
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className={`select ${open ? "open" : ""} ${className}`} ref={box}>
      <button
        type="button"
        className="select-button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={`${id}-list`}
        aria-activedescendant={open ? `${id}-${hi}` : undefined}
        aria-label={label}
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : show())}
        onKeyDown={onKey}
      >
        <span className="select-value">{current?.label}</span>
        <span className="select-chev" aria-hidden="true">
          <Icon.chevron />
        </span>
      </button>
      {open && (
        <ul id={`${id}-list`} className={`select-menu ${up ? "up" : ""}`} role="listbox" aria-label={label}>
          {options.map((o, i) => (
            <li
              key={o.value}
              id={`${id}-${i}`}
              role="option"
              aria-selected={o.value === value}
              className={i === hi ? "active" : ""}
              onMouseEnter={() => setHi(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => choose(i)}
            >
              <span className="grow">{o.label}</span>
              {o.value === value && <Icon.check />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ComboInput({ value, onChange, suggestions, placeholder, label }: {
  value: string;
  onChange: (v: string) => void;
  suggestions: string[];
  placeholder?: string;
  label?: string;
}) {
  const id = useId();
  const box = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(-1);
  const q = value.trim().toLowerCase();
  // everything on an empty field or one that holds a listed value; otherwise what contains the typed text
  const shown = !q || suggestions.includes(value) ? suggestions : suggestions.filter((s) => s.toLowerCase().includes(q));
  const visible = open && shown.length > 0;
  const up = useFlip(box, visible, shown.length);
  useOutside(box, open, () => setOpen(false));
  useEffect(() => setHi(-1), [q, open]);

  const onKey = (e: KeyboardEvent) => {
    if (!visible) {
      if (e.key === "ArrowDown") setOpen(true);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setHi((i) => (e.key === "ArrowDown" ? (i + 1) % shown.length : (i - 1 + shown.length) % shown.length));
    } else if (e.key === "Enter" && hi >= 0) {
      e.preventDefault();
      onChange(shown[hi]);
      setOpen(false);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
    }
  };

  return (
    <div className={`select combo ${visible ? "open" : ""}`} ref={box}>
      <input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
        placeholder={placeholder}
        aria-label={label}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={visible}
        aria-controls={`${id}-list`}
        aria-activedescendant={hi >= 0 ? `${id}-${hi}` : undefined}
        autoComplete="off"
      />
      {visible && (
        <ul id={`${id}-list`} className={`select-menu ${up ? "up" : ""}`} role="listbox" aria-label={label}>
          {shown.map((s, i) => (
            <li
              key={s}
              id={`${id}-${i}`}
              role="option"
              aria-selected={s === value}
              className={i === hi ? "active" : ""}
              onMouseEnter={() => setHi(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                onChange(s);
                setOpen(false);
              }}
            >
              <span className="grow">{s}</span>
              {s === value && <Icon.check />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
