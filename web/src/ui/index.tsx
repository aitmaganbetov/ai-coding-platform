import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { AlertTriangle, Check, ChevronDown, ChevronUp, Copy, Inbox, Loader2, RefreshCw, Search, X } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  forwardRef,
  type KeyboardEvent,
  type ReactNode,
  useMemo,
  useRef,
  useState,
} from "react";
import { create } from "zustand";

// NEXUS design system: every page builds on these. Colours, sizes and motion come from
// the tokens in theme.css; states (hover, focus, active, disabled, loading, error) are
// handled here once.

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

// ---------------------------------------------------------------- buttons --
type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  icon?: ReactNode;
  loading?: boolean;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", icon, loading, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx("btn", `btn-${variant}`, `btn-${size}`, !children && "btn-icon", className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Loader2 className="spin" size={14} aria-hidden /> : icon}
      {children}
    </button>
  );
});

export function IconButton({ label, icon, ...rest }: ButtonProps & { label: string; icon: ReactNode }) {
  return (
    <Tooltip label={label}>
      <Button variant="ghost" size="sm" aria-label={label} icon={icon} {...rest} />
    </Tooltip>
  );
}

export function LinkButton({
  href,
  variant = "secondary",
  size = "md",
  icon,
  children,
  external,
}: {
  href: string;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
  icon?: ReactNode;
  children: ReactNode;
  external?: boolean;
}) {
  return (
    <a
      href={href}
      className={cx("btn", `btn-${variant}`, `btn-${size}`)}
      {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
    >
      {icon}
      {children}
    </a>
  );
}

// ------------------------------------------------------------ badges, state --
export function Badge({ tone = "neutral", dot, children, title }: { tone?: Tone; dot?: boolean; children: ReactNode; title?: string }) {
  return (
    <span className={cx("badge", `tone-${tone}`)} title={title}>
      {dot && <span className="badge-dot" aria-hidden />}
      {children}
    </span>
  );
}

export const RUN_STATES: Record<string, { label: string; tone: Tone; active: boolean }> = {
  CREATED: { label: "Создан", tone: "neutral", active: true },
  DISCOVERY: { label: "Анализ", tone: "info", active: true },
  PLANNED: { label: "Запланирован", tone: "info", active: true },
  AWAITING_APPROVAL: { label: "Ждёт согласия", tone: "warning", active: true },
  RUNNING: { label: "Выполняется", tone: "accent", active: true },
  VERIFYING: { label: "Проверка", tone: "accent", active: true },
  PAUSED: { label: "Пауза", tone: "warning", active: true },
  COMPLETED: { label: "Завершён", tone: "success", active: false },
  FAILED: { label: "Ошибка", tone: "danger", active: false },
  CANCELLED: { label: "Отменён", tone: "neutral", active: false },
};

export function isActiveRun(state: string): boolean {
  return RUN_STATES[state]?.active ?? false;
}

export function StateBadge({ state }: { state: string }) {
  const info = RUN_STATES[state] ?? { label: state, tone: "neutral" as Tone, active: false };
  return (
    <span className={cx("badge", `tone-${info.tone}`, info.active && "is-live")} title={state}>
      <span className="badge-dot" aria-hidden />
      {info.label}
    </span>
  );
}

export const SEVERITIES = ["critical", "high", "medium", "low", "info"] as const;
const SEVERITY_TONE: Record<string, Tone> = { critical: "danger", high: "danger", medium: "warning", low: "info", info: "neutral" };

export function SeverityBadge({ severity }: { severity: string }) {
  const value = severity.toLowerCase();
  return (
    <span className={cx("badge", "badge-severity", `tone-${SEVERITY_TONE[value] ?? "neutral"}`, value === "critical" && "is-critical")}>
      {value}
    </span>
  );
}

const GATE_TONE: Record<string, Tone> = { PASS: "success", FAIL: "danger", SKIPPED: "neutral", WARN: "warning" };

export function GateBadge({ result }: { result: string }) {
  return <Badge tone={GATE_TONE[result] ?? "neutral"} dot>{result}</Badge>;
}

export function StatusDot({ tone, label, pulse }: { tone: Tone; label: string; pulse?: boolean }) {
  return (
    <span className={cx("status-dot", `tone-${tone}`, pulse && "pulse")} role="img" aria-label={label} title={label} />
  );
}

// ------------------------------------------------------------------ cards --
export function Card({
  title,
  icon,
  actions,
  href,
  children,
  className,
  footer,
  span,
}: {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  href?: string;
  children?: ReactNode;
  className?: string;
  footer?: ReactNode;
  span?: 1 | 2 | 3 | 4;
}) {
  const head = (title || actions) && (
    <div className="card-head">
      {icon && <span className="card-icon">{icon}</span>}
      {title && <h3 className="card-title">{title}</h3>}
      <span className="card-spacer" />
      {actions}
    </div>
  );
  return (
    <section className={cx("panel", href && "panel-link", span && `span-${span}`, className)}>
      {head}
      {href ? (
        <a className="panel-body panel-anchor" href={href}>
          {children}
        </a>
      ) : (
        <div className="panel-body">{children}</div>
      )}
      {footer && <div className="panel-foot">{footer}</div>}
    </section>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: Tone }) {
  return (
    <div className={cx("stat", tone && `tone-${tone}`)}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  );
}

export function Progress({ value, max = 100, tone = "accent", label }: { value: number; max?: number; tone?: Tone; label: string }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className={cx("progress", `tone-${tone}`)} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

export function PageHeader({ title, subtitle, actions, icon }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; icon?: ReactNode }) {
  return (
    <header className="page-header">
      {icon && <span className="page-icon">{icon}</span>}
      <div className="page-titles">
        <h1>{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

// ------------------------------------------------------------------- tabs --
export interface TabItem {
  id: string;
  label: ReactNode;
  count?: number;
  icon?: ReactNode;
  disabled?: boolean;
}

export function Tabs({ items, value, onChange, label }: { items: TabItem[]; value: string; onChange: (id: string) => void; label: string }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  function onKey(event: KeyboardEvent<HTMLDivElement>) {
    const enabled = items.filter((i) => !i.disabled);
    const index = enabled.findIndex((i) => i.id === value);
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step || index < 0) return;
    event.preventDefault();
    const next = enabled[(index + step + enabled.length) % enabled.length];
    if (next) {
      onChange(next.id);
      refs.current[items.indexOf(next)]?.focus();
    }
  }
  return (
    <div className="tabs" role="tablist" aria-label={label} onKeyDown={onKey}>
      {items.map((item, i) => (
        <button
          key={item.id}
          ref={(el) => (refs.current[i] = el)}
          type="button"
          role="tab"
          aria-selected={item.id === value}
          tabIndex={item.id === value ? 0 : -1}
          disabled={item.disabled}
          className={cx("tab", item.id === value && "active")}
          onClick={() => onChange(item.id)}
        >
          {item.icon}
          {item.label}
          {typeof item.count === "number" && <span className="tab-count">{item.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Segmented<T extends string>({ options, value, onChange, label }: { options: { value: T; label: ReactNode }[]; value: T; onChange: (v: T) => void; label: string }) {
  return (
    <div className="segmented-ctl" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={o.value === value} className={cx(o.value === value && "active")} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ----------------------------------------------------------------- inputs --
export function SearchInput({ value, onChange, placeholder, label }: { value: string; onChange: (v: string) => void; placeholder?: string; label: string }) {
  return (
    <label className="search-input">
      <Search size={14} aria-hidden />
      <input type="search" aria-label={label} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      {value && (
        <button type="button" aria-label="Очистить" onClick={() => onChange("")}>
          <X size={12} />
        </button>
      )}
    </label>
  );
}

export function Select({ label, value, onChange, options, hideLabel }: { label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; hideLabel?: boolean }) {
  return (
    <label className={cx("field-select", hideLabel && "hide-label")}>
      <span className={hideLabel ? "sr-only" : "field-label"}>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

// ------------------------------------------------------------------ states --
export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state-empty">
      <span className="state-icon">{icon ?? <Inbox size={20} />}</span>
      <strong>{title}</strong>
      {children && <div className="state-text">{children}</div>}
      {action && <div className="state-action">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "Не удалось загрузить данные" }: { error: unknown; onRetry?: () => void; title?: string }) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return (
    <div className="state-error" role="alert">
      <AlertTriangle size={18} aria-hidden />
      <div>
        <strong>{title}</strong>
        {message && <div className="state-text">{message}</div>}
      </div>
      {onRetry && (
        <Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} onClick={onRetry}>
          Повторить
        </Button>
      )}
    </div>
  );
}

export function Skeleton({ lines = 3, height }: { lines?: number; height?: number }) {
  if (height) return <div className="skeleton" style={{ height }} aria-hidden />;
  return (
    <div className="skeleton-lines" aria-busy="true" aria-label="Загрузка">
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="skeleton" style={{ width: `${90 - i * 13}%` }} />
      ))}
    </div>
  );
}

export function Spinner({ label = "Загрузка" }: { label?: string }) {
  return <Loader2 className="spin" size={16} role="img" aria-label={label} />;
}

/** One place that shows loading, error and empty states of a query. */
export function QueryState<T>({
  query,
  empty,
  children,
  skeleton,
}: {
  query: { data: T | undefined; error: unknown; isLoading: boolean; refetch: () => unknown };
  empty?: (data: T) => ReactNode | null;
  children: (data: T) => ReactNode;
  skeleton?: ReactNode;
}) {
  if (query.isLoading) return <>{skeleton ?? <Skeleton />}</>;
  if (query.error) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (query.data === undefined) return null;
  const nothing = empty?.(query.data);
  return <>{nothing ?? children(query.data)}</>;
}

// ------------------------------------------------------------------- table --
export interface Column<T> {
  id: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  sort?: (row: T) => string | number;
  width?: string;
  className?: string;
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  onRowClick,
  label,
  initialSort,
  empty,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  label: string;
  initialSort?: { id: string; desc?: boolean };
  empty?: ReactNode;
}) {
  const [sort, setSort] = useState(initialSort ?? null);
  const sorted = useMemo(() => {
    const column = sort && columns.find((c) => c.id === sort.id);
    if (!column?.sort) return rows;
    const by = column.sort;
    return [...rows].sort((a, b) => {
      const x = by(a);
      const y = by(b);
      const order = x < y ? -1 : x > y ? 1 : 0;
      return sort?.desc ? -order : order;
    });
  }, [rows, columns, sort]);
  if (rows.length === 0) return <>{empty ?? <EmptyState title="Пока пусто" />}</>;
  return (
    <div className="table-wrap" role="region" aria-label={label} tabIndex={0}>
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.id} style={c.width ? { width: c.width } : undefined} className={c.className} aria-sort={sort?.id === c.id ? (sort.desc ? "descending" : "ascending") : undefined}>
                {c.sort ? (
                  <button type="button" className="th-sort" onClick={() => setSort((s) => ({ id: c.id, desc: s?.id === c.id ? !s.desc : false }))}>
                    {c.header}
                    {sort?.id === c.id && (sort.desc ? <ChevronDown size={12} /> : <ChevronUp size={12} />)}
                  </button>
                ) : (
                  c.header
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr
              key={rowKey(row)}
              className={onRowClick ? "clickable" : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={onRowClick ? (e) => e.key === "Enter" && onRowClick(row) : undefined}
              tabIndex={onRowClick ? 0 : undefined}
            >
              {columns.map((c) => (
                <td key={c.id} className={c.className}>
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------- code, copy --
export function CopyButton({ text, label = "Копировать" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <IconButton
      label={done ? "Скопировано" : label}
      icon={done ? <Check size={13} /> : <Copy size={13} />}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(
          () => {
            setDone(true);
            window.setTimeout(() => setDone(false), 1500);
          },
          () => toast({ tone: "danger", title: "Не удалось скопировать" }),
        );
      }}
    />
  );
}

export function CodeBlock({ code, label }: { code: string; label?: string }) {
  return (
    <div className="code-block">
      <div className="code-block-bar">
        <span>{label}</span>
        <CopyButton text={code} />
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

// ------------------------------------------------- overlays (Radix-based) --
export function Tooltip({ label, children, side = "top" }: { label: ReactNode; children: ReactNode; side?: "top" | "right" | "bottom" | "left" }) {
  return (
    <TooltipPrimitive.Root delayDuration={350}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content className="tooltip" side={side} sideOffset={6}>
          {label}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

export const TooltipProvider = TooltipPrimitive.Provider;

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="overlay" />
        <DialogPrimitive.Content className={cx("dialog", wide && "dialog-wide")} aria-describedby={description ? undefined : undefined}>
          <div className="dialog-head">
            <DialogPrimitive.Title className="dialog-title">{title}</DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <button type="button" className="btn btn-ghost btn-sm btn-icon" aria-label="Закрыть">
                <X size={14} />
              </button>
            </DialogPrimitive.Close>
          </div>
          {description ? (
            <DialogPrimitive.Description className="dialog-desc">{description}</DialogPrimitive.Description>
          ) : (
            <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
          )}
          <div className="dialog-body">{children}</div>
          {footer && <div className="dialog-foot">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

export function DropdownMenu({ trigger, children, align = "end" }: { trigger: ReactNode; children: ReactNode; align?: "start" | "end" }) {
  return (
    <Menu.Root>
      <Menu.Trigger asChild>{trigger}</Menu.Trigger>
      <Menu.Portal>
        <Menu.Content className="menu" align={align} sideOffset={6}>
          {children}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}

export function MenuItem({ children, onSelect, icon, danger }: { children: ReactNode; onSelect: () => void; icon?: ReactNode; danger?: boolean }) {
  return (
    <Menu.Item className={cx("menu-item", danger && "danger")} onSelect={onSelect}>
      {icon}
      {children}
    </Menu.Item>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <Menu.Label className="menu-label">{children}</Menu.Label>;
}

export const MenuSeparator = () => <Menu.Separator className="menu-sep" />;

// ----------------------------------------------------------------- toasts --
interface ToastItem {
  id: number;
  tone: Tone;
  title: string;
  text?: string;
}

const useToasts = create<{ items: ToastItem[]; push: (t: Omit<ToastItem, "id">) => void; drop: (id: number) => void }>((set) => ({
  items: [],
  push: (t) => {
    const id = Date.now() + Math.random();
    set((s) => ({ items: [...s.items.slice(-3), { ...t, id }] }));
    window.setTimeout(() => set((s) => ({ items: s.items.filter((i) => i.id !== id) })), 5000);
  },
  drop: (id) => set((s) => ({ items: s.items.filter((i) => i.id !== id) })),
}));

export function toast(t: Omit<ToastItem, "id">): void {
  useToasts.getState().push(t);
}

export function Toaster() {
  const { items, drop } = useToasts();
  return (
    <div className="toaster" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={cx("toast", `tone-${t.tone}`)} role="status">
          <div>
            <strong>{t.title}</strong>
            {t.text && <div className="state-text">{t.text}</div>}
          </div>
          <button type="button" aria-label="Закрыть уведомление" onClick={() => drop(t.id)}>
            <X size={12} />
          </button>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- helpers --
const RTF = typeof Intl !== "undefined" && "RelativeTimeFormat" in Intl ? new Intl.RelativeTimeFormat("ru", { numeric: "auto" }) : null;

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((then - now) / 1000);
  const abs = Math.abs(seconds);
  if (!RTF) return new Date(iso).toLocaleString("ru");
  if (abs < 45) return RTF.format(seconds, "second");
  if (abs < 2700) return RTF.format(Math.round(seconds / 60), "minute");
  if (abs < 79200) return RTF.format(Math.round(seconds / 3600), "hour");
  if (abs < 2592000) return RTF.format(Math.round(seconds / 86400), "day");
  return new Date(iso).toLocaleDateString("ru");
}

export function duration(start: string | null | undefined, end: string | null | undefined, now = Date.now()): string {
  if (!start) return "—";
  const ms = (end ? new Date(end).getTime() : now) - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} с`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} мин ${s % 60} с`;
  return `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

export function formatNumber(n: number): string {
  return new Intl.NumberFormat("ru", { notation: n >= 10_000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(n);
}

export function formatCost(usd: number): string {
  return `$${usd < 1 ? usd.toFixed(4) : usd.toFixed(2)}`;
}

export function shortId(id: string): string {
  return id.slice(-8);
}

export { cx };
