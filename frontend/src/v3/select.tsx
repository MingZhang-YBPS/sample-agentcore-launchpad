// The V3 dropdown. A drop-in for a native `<select>`: same `value` / `onChange(e)` /
// `<option>` / `<optgroup>` children, so a page swaps `<select` for `<Select` and
// nothing else changes. The trigger is a button styled by the class it is given
// (`v3-select` / `v3-input`) and the list is drawn by the console instead of the OS,
// so it follows the V3 theme. Styling lives in v3.css (`.v3-sel`, `.v3-pop`).
import { Check, ChevronDown, Search } from "lucide-react";
import {
  Children,
  type CSSProperties,
  Fragment,
  isValidElement,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
  title?: string;
  /** the `<optgroup label>` it sits under */
  group?: string;
}

/** What a native change handler reads: `e.target.value` (and `name`). */
export interface SelectChange {
  target: { value: string; name?: string };
  currentTarget: { value: string; name?: string };
}

/** Lists longer than this get a filter box at the top of the popup. */
const SEARCH_THRESHOLD = 8;

type ElementProps = { children?: ReactNode; value?: unknown; label?: unknown; disabled?: boolean; title?: string };

function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<ElementProps>(node)) return textOf(node.props.children);
  return "";
}

/** The `<option>`s under `children`, through fragments and `<optgroup>`s, in order. */
function optionsOf(children: ReactNode, group?: string, out: SelectOption[] = []): SelectOption[] {
  Children.forEach(children, (child) => {
    if (!isValidElement<ElementProps>(child)) return;
    if (child.type === Fragment) optionsOf(child.props.children, group, out);
    else if (child.type === "optgroup") optionsOf(child.props.children, String(child.props.label ?? ""), out);
    else if (child.type === "option") {
      const label = textOf(child.props.children) || String(child.props.label ?? "");
      out.push({
        value: child.props.value == null ? label : String(child.props.value),
        label,
        disabled: Boolean(child.props.disabled),
        title: child.props.title,
        group,
      });
    }
  });
  return out;
}

export function Select({
  value,
  onChange,
  children,
  disabled,
  searchable,
  className,
  style,
  id,
  name,
  title,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  "aria-describedby": ariaDescribedBy,
  "aria-invalid": ariaInvalid,
  "data-testid": testId,
}: {
  value?: string | number;
  onChange?: (e: SelectChange) => void;
  /** `<option>` / `<optgroup>` elements, as under a native select */
  children: ReactNode;
  disabled?: boolean;
  /** default: on when there are more than 8 options */
  searchable?: boolean;
  className?: string;
  style?: CSSProperties;
  id?: string;
  /** also renders a hidden input, so a surrounding form still reads the value */
  name?: string;
  title?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "true" | "false";
  "data-testid"?: string;
}) {
  const { t } = useTranslation();
  const options = useMemo(() => optionsOf(children), [children]);
  const current = value == null ? "" : String(value);
  const listId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const typeahead = useRef({ text: "", at: 0 });
  // Space-select closes the list on keydown, so by keyup `open` is already false
  const spaceChose = useRef(false);
  const [openState, setOpen] = useState(false);
  // a control disabled while its list is open (e.g. a request starting) drops the list
  const open = openState && !disabled;
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const [pos, setPos] = useState<CSSProperties>({});
  const withSearch = searchable ?? options.length > SEARCH_THRESHOLD;
  const needle = query.trim().toLowerCase();
  const visible = useMemo(
    () => (needle ? options.filter((o) => o.label.toLowerCase().includes(needle)) : options),
    [options, needle],
  );

  // like a native select, a value no option carries shows the first option
  const shown = options.find((o) => o.value === current) ?? options[0];

  const place = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 8;
    const above = r.top - 8;
    // flip only when the list (capped at 280 px) does not fit below
    const need = Math.min(280, options.length * 34 + (withSearch ? 46 : 0) + 10);
    const up = below < need && above > below;
    setPos({
      left: r.left,
      minWidth: r.width,
      maxWidth: Math.max(r.width, Math.min(480, window.innerWidth - r.left - 8)),
      maxHeight: Math.min(320, Math.max(120, up ? above : below)),
      ...(up ? { bottom: window.innerHeight - r.top + 6 } : { top: r.bottom + 6 }),
    });
  }, [options.length, withSearch]);

  const step = useCallback(
    (from: number, dir: 1 | -1) => {
      for (let i = from + dir; i >= 0 && i < visible.length; i += dir) {
        if (!visible[i].disabled) return i;
      }
      return from;
    },
    [visible],
  );

  const show = () => {
    if (disabled) return;
    const at = options.findIndex((o) => o.value === current && !o.disabled);
    setQuery("");
    setActive(at >= 0 ? at : options.findIndex((o) => !o.disabled));
    place();
    setOpen(true);
  };

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  }, []);

  const choose = (o: SelectOption | undefined) => {
    if (!o || o.disabled) return;
    if (o.value !== current && onChange) {
      const target = { value: o.value, name };
      onChange({ target, currentTarget: target });
    }
    close(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target) || popRef.current?.contains(target)) return;
      close(false);
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    searchRef.current?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, close, place]);

  useEffect(() => {
    if (!open || active < 0) return;
    popRef.current?.querySelector(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    if (disabled) return;
    const inSearch = e.target === searchRef.current;
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        show();
      }
      return; // Enter / Space fall through to the trigger's click
    }
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setActive((i) => step(i, 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setActive((i) => step(i, -1));
        break;
      case "Home":
      case "End":
        if (inSearch) return;
        e.preventDefault();
        setActive(e.key === "Home" ? step(-1, 1) : step(visible.length, -1));
        break;
      case "Enter":
        e.preventDefault();
        choose(visible[active]);
        break;
      case " ":
        if (inSearch) return;
        e.preventDefault();
        spaceChose.current = true;
        choose(visible[active]);
        break;
      case "Escape":
        // keep an enclosing Dialog / Confirm (window keydown listener) open
        e.preventDefault();
        e.stopPropagation();
        close(true);
        break;
      case "Tab":
        close(false);
        break;
      default: {
        if (inSearch || e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
        const now = Date.now();
        const ta = typeahead.current;
        ta.text = now - ta.at > 600 ? e.key.toLowerCase() : ta.text + e.key.toLowerCase();
        ta.at = now;
        const hit = visible.findIndex((o) => !o.disabled && o.label.toLowerCase().startsWith(ta.text));
        if (hit >= 0) setActive(hit);
      }
    }
  };

  const optionId = (i: number) => `${listId}-o${i}`;
  // like a native select, only the first option carrying the value is "the" selection
  const selectedIdx = visible.findIndex((o) => o.value === current);
  let lastGroup: string | undefined;

  return (
    <>
      {/* eslint-disable-next-line launchpad/disabled-reason -- the primitive: a caller wraps it in <Reasoned> */}
      <button
        ref={triggerRef}
        type="button"
        id={id}
        className={["v3-sel", className ?? "v3-select", open ? "open" : ""].join(" ").replace(/\s+/g, " ").trim()}
        style={style}
        title={title ?? shown?.title}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && !withSearch && active >= 0 ? optionId(active) : undefined}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        aria-describedby={ariaDescribedBy}
        aria-invalid={ariaInvalid}
        data-testid={testId}
        data-value={current}
        onClick={() => (open ? close(false) : show())}
        onKeyDown={onKeyDown}
        onKeyUp={(e) => {
          if (e.key !== " ") return;
          if (open || spaceChose.current) e.preventDefault(); // no click-reopen after Space-select
          spaceChose.current = false;
        }}
      >
        <span className="val">{shown?.label ?? ""}</span>
        <ChevronDown className="chev" size={15} aria-hidden="true" />
      </button>
      {name && <input type="hidden" name={name} value={current} />}
      {open && (
        <div
          ref={popRef}
          className="v3-pop"
          style={pos}
          onKeyDown={onKeyDown}
          // inside a <label>, a click here would re-activate the trigger and reopen the list
          onClick={(e) => e.preventDefault()}
          data-testid="v3-select-popup"
        >
          {withSearch && (
            <div className="v3-pop-search">
              <Search size={14} aria-hidden="true" />
              <input
                ref={searchRef}
                value={query}
                placeholder={t("v3.common.searchOptions")}
                aria-label={t("v3.common.searchOptions")}
                aria-controls={listId}
                aria-activedescendant={active >= 0 ? optionId(active) : undefined}
                onChange={(e) => {
                  const q = e.target.value.trim().toLowerCase();
                  setQuery(e.target.value);
                  setActive(options.filter((o) => !q || o.label.toLowerCase().includes(q)).findIndex((o) => !o.disabled));
                }}
              />
            </div>
          )}
          <div className="v3-pop-list" role="listbox" id={listId} aria-label={ariaLabel}>
            {visible.length === 0 && <div className="v3-pop-empty">{t("v3.common.noMatch")}</div>}
            {visible.map((o, i) => {
              const header = o.group && o.group !== lastGroup ? o.group : null;
              lastGroup = o.group;
              const selected = i === selectedIdx;
              return (
                <Fragment key={i}>
                  {header && (
                    <div className="v3-pop-group" role="presentation">
                      {header}
                    </div>
                  )}
                  <div
                    id={optionId(i)}
                    role="option"
                    aria-selected={selected}
                    aria-disabled={o.disabled || undefined}
                    data-idx={i}
                    data-value={o.value}
                    className={["v3-opt", o.group ? "grouped" : "", i === active ? "active" : "", selected ? "selected" : "", o.disabled ? "disabled" : ""].join(" ").replace(/\s+/g, " ").trim()}
                    title={o.title ?? o.label}
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseEnter={() => !o.disabled && setActive(i)}
                    onClick={() => choose(o)}
                  >
                    <span className="txt">{o.label}</span>
                    {selected && <Check size={14} aria-hidden="true" />}
                  </div>
                </Fragment>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}
