import {
  Accessibility,
  Boxes,
  Database,
  FileCode2,
  FileText,
  Gauge,
  GitCompare,
  LayoutGrid,
  Network,
  ShieldCheck,
  SquareCode,
  Wrench,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { api, errorText, type FindingRow, type GlobalRun } from "../api";
import { queryClient, useFindings, useProjects, useRuns } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  type Column,
  DataTable,
  Dialog,
  EmptyState,
  LinkButton,
  PageHeader,
  QueryState,
  relativeTime,
  SearchInput,
  Select,
  SEVERITIES,
  SeverityBadge,
  shortId,
  Skeleton,
  StateBadge,
  Stat,
  Tabs,
  toast,
} from "../ui";
import "./quality.css";

// Audit Center: the findings of the platform's analysers (and agents), by category, with
// evidence, history and a way to hand a fix to the chat. Every audit records its findings
// anew: the overview and the categories show each project's latest audit (the API's
// "current" findings), the trend and a finding's history every audit. Nothing here is
// computed beyond grouping the findings the API returns.

export type Category =
  | "security"
  | "dependencies"
  | "quality"
  | "performance"
  | "accessibility"
  | "database"
  | "architecture"
  | "other";

// Order matters: the first category whose sources match wins.
const SOURCES: [Exclude<Category, "other">, string[]][] = [
  ["dependencies", ["osv-scanner", "osv", "trivy", "npm-audit", "pip-audit", "composer-audit", "cargo-audit"]],
  ["quality", ["ruff", "mypy", "eslint", "tsc", "phpstan", "pint", "golangci-lint", "clippy", "reviewer"]],
  ["performance", ["k6", "performance", "lighthouse-perf"]],
  ["accessibility", ["playwright", "axe", "lighthouse", "accessibility", "a11y"]],
  ["database", ["database", "migration", "sql"]],
  ["architecture", ["architect", "architecture"]],
  ["security", ["semgrep", "gitleaks", "bandit", "trufflehog", "security", "secret"]],
];

export function categoryOf(source: string): Category {
  const value = source.toLowerCase();
  for (const [category, names] of SOURCES) {
    if (names.some((n) => value === n || value.includes(n))) return category;
  }
  return "other";
}

const CATEGORY_INFO: Record<Exclude<Category, "other">, { label: string; icon: ReactNode; empty: string }> = {
  quality: {
    label: "Качество кода",
    icon: <SquareCode size={14} />,
    empty: "Замечаний линтеров и проверки типов нет (Ruff, mypy, ESLint, tsc, PHPStan, golangci-lint, Clippy — по стеку проекта).",
  },
  security: {
    label: "Безопасность",
    icon: <ShieldCheck size={14} />,
    empty: "Находок SAST и секретов нет (Semgrep, Gitleaks).",
  },
  dependencies: {
    label: "Зависимости",
    icon: <Boxes size={14} />,
    empty: "Уязвимых зависимостей не найдено (OSV-Scanner, Trivy). Без офлайн-баз уязвимостей эти анализаторы пропускаются — проверьте «Инфраструктуру».",
  },
  database: {
    label: "База данных",
    icon: <Database size={14} />,
    empty: "Детерминированного анализатора БД в платформе нет. Находки по миграциям, индексам и транзакциям даёт агент Database в run'ах с ИИ-провайдером проекта.",
  },
  performance: {
    label: "Производительность",
    icon: <Gauge size={14} />,
    empty: "Находок производительности нет. Их даёт k6, если в проекте есть сценарии (*.k6.js, k6/*.js), и агент Performance.",
  },
  architecture: {
    label: "Архитектура",
    icon: <Network size={14} />,
    empty: "Архитектурные замечания даёт агент Architect (ADR, схема модулей) в run'ах с ИИ-провайдером; анализатора архитектуры среди инструментов аудита нет.",
  },
  accessibility: {
    label: "Доступность",
    icon: <Accessibility size={14} />,
    empty: "Находок доступности нет. Их дают E2E-проверки Playwright, если они настроены в проекте.",
  },
};

const SEVERITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
const rank = (f: FindingRow) => SEVERITY_RANK[f.severity.toLowerCase()] ?? 9;

/** Identity of a finding across runs: its fingerprint, or its location if there is none. */
export function findingKey(f: FindingRow): string {
  return f.fingerprint || [f.source, f.rule_id ?? "", f.file_path ?? "", f.line ?? "", f.title].join("|");
}

/** Findings of audit B against audit A: new in B, fixed since A, still present. */
export function compareAudits(a: FindingRow[], b: FindingRow[]) {
  const before = new Map(a.map((f) => [findingKey(f), f]));
  const after = new Map(b.map((f) => [findingKey(f), f]));
  return {
    added: [...after].filter(([k]) => !before.has(k)).map(([, f]) => f),
    fixed: [...before].filter(([k]) => !after.has(k)).map(([, f]) => f),
    kept: [...after].filter(([k]) => before.has(k)).map(([, f]) => f),
  };
}

export function fixDraft(f: FindingRow): string {
  const where = f.file_path ? `${f.file_path}${f.line ? `:${f.line}` : ""}` : "проекте";
  const rule = f.rule_id ? `, правило ${f.rule_id}` : "";
  return (
    `Исправь находку аудита «${f.title}» (${f.severity.toLowerCase()}, ${f.source}${rule}) в ${where}. ` +
    "Сначала воспроизведи проблему и покажи план исправления, затем исправь, добавь регрессионный тест " +
    "и запусти аудит платформы, чтобы проверить, что находка ушла."
  );
}

function location(f: FindingRow): string {
  if (!f.file_path) return "—";
  return f.line ? `${f.file_path}:${f.line}` : f.file_path;
}

// ------------------------------------------------------------------ table --
function FindingsTable({ rows, showProject, onOpen, empty }: { rows: FindingRow[]; showProject: boolean; onOpen: (f: FindingRow) => void; empty: ReactNode }) {
  const columns: Column<FindingRow>[] = [
    { id: "severity", header: "Важность", cell: (f) => <SeverityBadge severity={f.severity} />, sort: rank, width: "104px" },
    {
      id: "title",
      header: "Находка",
      cell: (f) => (
        <>
          <span className="cell-main">{f.title}</span>
          {f.rule_id && <span className="cell-sub mono">{f.rule_id}</span>}
        </>
      ),
      sort: (f) => f.title.toLowerCase(),
    },
    { id: "file", header: "Файл", cell: (f) => <span className="path">{location(f)}</span>, sort: (f) => location(f) },
    { id: "source", header: "Инструмент", cell: (f) => <Badge>{f.source}</Badge>, sort: (f) => f.source },
    ...(showProject ? [{ id: "project", header: "Проект", cell: (f: FindingRow) => f.project_name ?? "—", sort: (f: FindingRow) => f.project_name ?? "" }] : []),
    { id: "status", header: "Статус", cell: (f) => <Badge tone={f.status === "open" ? "warning" : "neutral"}>{f.status}</Badge>, sort: (f) => f.status },
    { id: "found", header: "Найдена", cell: (f) => <span className="text-muted">{relativeTime(f.created_at)}</span>, sort: (f) => f.created_at ?? "" },
  ];
  return (
    <DataTable
      label="Находки"
      rows={rows}
      columns={columns}
      rowKey={(f) => f.id}
      onRowClick={onOpen}
      initialSort={{ id: "severity" }}
      empty={empty}
    />
  );
}

// ----------------------------------------------------------------- dialog --
function FindingDialog({ finding, all, onClose }: { finding: FindingRow | null; all: FindingRow[]; onClose: () => void }) {
  const history = useMemo(() => {
    if (!finding) return [];
    const key = findingKey(finding);
    return all.filter((f) => f.project_id === finding.project_id && findingKey(f) === key);
  }, [finding, all]);
  if (!finding) return null;
  const firstSeen = history.map((f) => f.created_at ?? "").filter(Boolean).sort()[0] ?? finding.created_at;
  const runs = new Set(history.map((f) => f.run_id).filter(Boolean));
  const evidence = finding.evidence && Object.keys(finding.evidence).length > 0 ? JSON.stringify(finding.evidence, null, 2) : null;
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={finding.title}
      wide
      footer={
        <>
          {finding.run_id && <LinkButton href={href("run", finding.run_id)}>Открыть run</LinkButton>}
          {finding.project_id && (
            <LinkButton variant="primary" icon={<Wrench size={14} />} href={href("ai", finding.project_id, { mode: "maintain", draft: fixDraft(finding) })}>
              Исправить в чате
            </LinkButton>
          )}
        </>
      }
    >
      <div className="finding-detail">
        <dl className="kv">
          <dt>Важность</dt>
          <dd>
            <SeverityBadge severity={finding.severity} />
          </dd>
          <dt>Файл</dt>
          <dd className="path">{location(finding)}</dd>
          <dt>Инструмент</dt>
          <dd>
            {finding.source} · категория «{categoryLabel(categoryOf(finding.source))}»
          </dd>
          <dt>Правило</dt>
          <dd className="mono">{finding.rule_id ?? "—"}</dd>
          <dt>Статус</dt>
          <dd>{finding.status}</dd>
          {finding.project_name && (
            <>
              <dt>Проект</dt>
              <dd>{finding.project_name}</dd>
            </>
          )}
        </dl>
        {finding.message && <p className="finding-message">{finding.message}</p>}
        {evidence ? <CodeBlock code={evidence} label="Доказательства" /> : <p className="text-muted">Инструмент не приложил доказательств к находке.</p>}
        <section className="finding-history" aria-label="История">
          <h4>История</h4>
          <p className="text-secondary">
            Впервые найдена {relativeTime(firstSeen)} · встречается в {Math.max(runs.size, 1)} run'ах · статусы:{" "}
            {[...new Set(history.map((f) => f.status))].join(", ") || finding.status}
          </p>
        </section>
      </div>
    </Dialog>
  );
}

function categoryLabel(category: Category): string {
  return category === "other" ? "прочее" : CATEGORY_INFO[category].label;
}

// --------------------------------------------------------------- overview --
function Overview({
  rows,
  history,
  audits,
  onCategory,
}: {
  rows: FindingRow[]; // the latest audit of each project
  history: FindingRow[]; // every audit's findings
  audits: GlobalRun[];
  onCategory: (c: string) => void;
}) {
  const open = rows.filter((f) => f.status === "open");
  const baselined = rows.filter((f) => f.status === "baselined").length;
  const byCategory = (Object.keys(CATEGORY_INFO) as Exclude<Category, "other">[]).map((c) => ({ c, n: open.filter((f) => categoryOf(f.source) === c).length }));
  const other = open.filter((f) => categoryOf(f.source) === "other").length;
  const files = new Map<string, number>();
  for (const f of open) if (f.file_path) files.set(f.file_path, (files.get(f.file_path) ?? 0) + 1);
  const topFiles = [...files].sort((a, b) => b[1] - a[1]).slice(0, 8);
  const trend = audits
    .slice(0, 10)
    .reverse()
    .map((r) => ({ run: r, n: history.filter((f) => f.run_id === r.id).length }));
  const max = Math.max(1, ...trend.map((t) => t.n));
  return (
    <div className="bento">
      <Card span={2} title="Открытые находки по важности" icon={<ShieldCheck size={15} />}>
        <div className="stats">
          {SEVERITIES.map((s) => (
            <Stat key={s} label={s} value={open.filter((f) => f.severity.toLowerCase() === s).length} tone={s === "critical" || s === "high" ? "danger" : s === "medium" ? "warning" : undefined} />
          ))}
        </div>
        <p className="note text-muted">
          По последнему аудиту каждого проекта{baselined > 0 ? ` · ещё ${baselined} принято через baseline` : ""}.
        </p>
      </Card>
      <Card span={2} title="По категориям" icon={<LayoutGrid size={15} />}>
        <div className="cat-grid">
          {byCategory.map(({ c, n }) => (
            <button key={c} type="button" className="cat-cell" onClick={() => onCategory(c)}>
              <span className="cat-icon">{CATEGORY_INFO[c].icon}</span>
              <span className="truncate">{CATEGORY_INFO[c].label}</span>
              <span className="mono cat-count">{n}</span>
            </button>
          ))}
          {other > 0 && (
            <span className="cat-cell static">
              <span className="truncate">Прочее</span>
              <span className="mono cat-count">{other}</span>
            </span>
          )}
        </div>
      </Card>
      <Card span={2} title="Файлы с наибольшим числом находок" icon={<FileCode2 size={15} />}>
        {topFiles.length === 0 ? (
          <p className="text-muted tile-empty">Открытых находок с привязкой к файлам нет.</p>
        ) : (
          <ol className="top-files">
            {topFiles.map(([file, n]) => (
              <li key={file}>
                <span className="path">{file}</span>
                <span className="mono">{n}</span>
              </li>
            ))}
          </ol>
        )}
      </Card>
      <Card span={2} title="Находки по последним аудитам" icon={<Gauge size={15} />}>
        {trend.length === 0 ? (
          <p className="text-muted tile-empty">Аудитов ещё не было. Запустите «Полный аудит».</p>
        ) : (
          <div className="trend-bars" role="list" aria-label="Число находок по аудитам">
            {trend.map(({ run, n }) => (
              <a key={run.id} role="listitem" href={href("run", run.id)} className="trend-bar" title={`${run.project_name} · ${new Date(run.created_at).toLocaleString("ru")}: ${n}`}>
                <span className="trend-fill" style={{ height: `${Math.max(4, (n / max) * 100)}%` }} />
                <span className="trend-count mono">{n}</span>
              </a>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- reports --
function Reports({ projectId }: { projectId: string }) {
  const runs = useRuns({ kind: "audit", project_id: projectId });
  const finished = (runs.data ?? []).filter((r) => r.state === "COMPLETED");
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  useEffect(() => {
    if (!b && finished[0]) setB(finished[0].id);
    if (!a && finished[1]) setA(finished[1].id);
  }, [finished, a, b]);
  const before = useFindings({ run_id: a || undefined, project_id: projectId });
  const after = useFindings({ run_id: b || undefined, project_id: projectId });
  const options = finished.map((r) => ({ value: r.id, label: `${new Date(r.created_at).toLocaleString("ru")} · ${shortId(r.id)}` }));
  const ready = a && b && a !== b && before.data && after.data;
  const diff = ready ? compareAudits(before.data ?? [], after.data ?? []) : null;
  return (
    <div className="stack">
      <Card title="Аудиты проекта" icon={<FileText size={15} />}>
        <QueryState
          query={runs}
          skeleton={<Skeleton lines={3} />}
          empty={(list) => (list.length === 0 ? <EmptyState title="Аудитов ещё не было">Запустите «Полный аудит» — отчёт JSON и Markdown появится в артефактах run'а.</EmptyState> : null)}
        >
          {(list) => (
            <DataTable
              label="Аудиты"
              rows={list}
              rowKey={(r) => r.id}
              onRowClick={(r) => (window.location.hash = href("run", r.id))}
              columns={[
                { id: "state", header: "Состояние", cell: (r) => <StateBadge state={r.state} />, width: "150px" },
                { id: "goal", header: "Цель", cell: (r) => <span className="cell-main">{r.goal || "audit"}</span> },
                { id: "by", header: "Запустил", cell: (r) => r.requested_by },
                { id: "when", header: "Когда", cell: (r) => relativeTime(r.created_at), sort: (r) => r.created_at },
              ]}
            />
          )}
        </QueryState>
      </Card>
      <Card title="Сравнить два аудита" icon={<GitCompare size={15} />}>
        {finished.length < 2 ? (
          <p className="text-muted tile-empty">Для сравнения нужны два завершённых аудита этого проекта.</p>
        ) : (
          <div className="stack">
            <div className="row">
              <Select label="Аудит A (раньше)" value={a} onChange={setA} options={[{ value: "", label: "—" }, ...options]} />
              <Select label="Аудит B (позже)" value={b} onChange={setB} options={[{ value: "", label: "—" }, ...options]} />
            </div>
            {a === b && a && <p className="text-muted">Выберите два разных аудита.</p>}
            {(before.isLoading || after.isLoading) && <Skeleton lines={2} />}
            {diff && (
              <>
                <div className="stats">
                  <Stat label="новые" value={diff.added.length} tone={diff.added.length ? "danger" : undefined} />
                  <Stat label="исправленные" value={diff.fixed.length} tone={diff.fixed.length ? "success" : undefined} />
                  <Stat label="сохранившиеся" value={diff.kept.length} />
                </div>
                <CompareList title="Новые" rows={diff.added} />
                <CompareList title="Исправленные" rows={diff.fixed} />
                <CompareList title="Сохранившиеся" rows={diff.kept} />
              </>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}

function CompareList({ title, rows }: { title: string; rows: FindingRow[] }) {
  if (rows.length === 0) return null;
  return (
    <section className="compare-list" aria-label={title}>
      <h4>
        {title} · {rows.length}
      </h4>
      <ul>
        {rows.slice(0, 50).map((f) => (
          <li key={findingKey(f)}>
            <SeverityBadge severity={f.severity} />
            <span className="truncate">{f.title}</span>
            <span className="path">{location(f)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------------- page --
const TABS = ["overview", "quality", "security", "dependencies", "database", "performance", "architecture", "accessibility", "reports"] as const;

export default function AuditCenter({ query }: { query: Record<string, string> }) {
  const currentProject = useUi((s) => s.projectId);
  const projects = useProjects();
  const [tab, setTab] = useState<string>(TABS.includes(query.category as (typeof TABS)[number]) ? query.category ?? "overview" : "overview");
  const [severity, setSeverity] = useState(query.severity ?? "");
  const [status, setStatus] = useState(query.status ?? "");
  const [source, setSource] = useState(query.source ?? "");
  const [project, setProject] = useState(query.project ?? currentProject ?? "");
  const [text, setText] = useState("");
  const [open, setOpen] = useState<FindingRow | null>(null);
  const [auditing, setAuditing] = useState(false);
  useEffect(() => {
    if (query.category && TABS.includes(query.category as (typeof TABS)[number])) setTab(query.category);
    if (query.severity !== undefined) setSeverity(query.severity);
  }, [query.category, query.severity]);

  const scope = project ? { project_id: project } : {};
  const findings = useFindings({ ...scope, current: true }); // each project's latest audit
  const history = useFindings(scope); // every audit: the trend, a finding's history
  const audits = useRuns(project ? { kind: "audit", project_id: project } : { kind: "audit" });
  const all = findings.data ?? [];
  const filtered = useMemo(() => {
    const words = text.toLowerCase().split(/\s+/).filter(Boolean);
    return all.filter(
      (f) =>
        (!severity || f.severity.toLowerCase() === severity) &&
        (!status || f.status === status) &&
        (!source || f.source === source) &&
        words.every((w) => `${f.title} ${f.file_path ?? ""} ${f.rule_id ?? ""} ${f.message ?? ""}`.toLowerCase().includes(w)),
    );
  }, [all, severity, status, source, text]);
  const inTab = tab === "overview" || tab === "reports" ? filtered : filtered.filter((f) => categoryOf(f.source) === tab);
  const count = (c: string) => filtered.filter((f) => categoryOf(f.source) === c).length;
  const target = project || currentProject;
  const targetName = projects.data?.find((p) => p.id === target)?.name;

  function fullAudit() {
    if (!target) return;
    setAuditing(true);
    api.createRun(target, "audit", {}).then(
      (run) => {
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: "success", title: "Полный аудит запущен", text: targetName });
        window.location.hash = href("run", run.id);
      },
      (err: unknown) => {
        toast({ tone: "danger", title: "Аудит не запущен", text: errorText(err) });
        setAuditing(false);
      },
    );
  }

  const tabs = [
    { id: "overview", label: "Обзор", icon: <LayoutGrid size={14} /> },
    ...(Object.keys(CATEGORY_INFO) as Exclude<Category, "other">[]).map((c) => ({ id: c, label: CATEGORY_INFO[c].label, icon: CATEGORY_INFO[c].icon, count: findings.data ? count(c) : undefined })),
    { id: "reports", label: "Отчёты", icon: <FileText size={14} /> },
  ];
  const sources = [...new Set(all.map((f) => f.source))].sort();
  const statuses = [...new Set(all.map((f) => f.status))].sort();

  return (
    <div className="page">
      <PageHeader
        icon={<ShieldCheck size={18} />}
        title="Audit Center"
        subtitle="Находки анализаторов платформы с доказательствами: безопасность, зависимости, качество кода и другие категории. Из любой находки можно поставить задачу на исправление в чат."
        actions={
          <Button variant="primary" icon={<ShieldCheck size={14} />} onClick={fullAudit} loading={auditing} disabled={!target} title={target ? undefined : "Выберите проект"}>
            Полный аудит{targetName ? ` «${targetName}»` : ""}
          </Button>
        }
      />
      <div className="audit-filters" role="search" aria-label="Фильтры находок">
        <Select
          label="Проект"
          value={project}
          onChange={setProject}
          options={[{ value: "", label: "Все проекты" }, ...(projects.data ?? []).map((p) => ({ value: p.id, label: p.name }))]}
        />
        <Select label="Важность" value={severity} onChange={setSeverity} options={[{ value: "", label: "Любая" }, ...SEVERITIES.map((s) => ({ value: s, label: s }))]} />
        <Select label="Статус" value={status} onChange={setStatus} options={[{ value: "", label: "Любой" }, ...statuses.map((s) => ({ value: s, label: s }))]} />
        <Select label="Инструмент" value={source} onChange={setSource} options={[{ value: "", label: "Любой" }, ...sources.map((s) => ({ value: s, label: s }))]} />
        <SearchInput label="Поиск по файлу, правилу и тексту" value={text} onChange={setText} placeholder="Файл, правило, текст…" />
      </div>
      <Tabs label="Категории аудита" items={tabs} value={tab} onChange={setTab} />
      {tab === "reports" ? (
        target ? (
          <Reports projectId={target} />
        ) : (
          <EmptyState title="Выберите проект">Отчёты и сравнение аудитов показываются по одному проекту.</EmptyState>
        )
      ) : (
        <QueryState query={findings} skeleton={<Skeleton lines={6} />}>
          {() =>
            tab === "overview" ? (
              <div className="stack">
                <Overview rows={filtered} history={history.data ?? []} audits={audits.data ?? []} onCategory={setTab} />
                <h3 className="section-title">Находки последнего аудита · {filtered.length}</h3>
                <FindingsTable
                  rows={filtered}
                  showProject={!project}
                  onOpen={setOpen}
                  empty={<EmptyState icon={<ShieldCheck size={20} />} title={all.length ? "Нет находок под этот фильтр" : "Находок нет"}>{all.length ? "Измените фильтры." : "После аудита здесь появятся находки анализаторов."}</EmptyState>}
                />
              </div>
            ) : (
              <FindingsTable
                rows={inTab}
                showProject={!project}
                onOpen={setOpen}
                empty={
                  <EmptyState icon={CATEGORY_INFO[tab as Exclude<Category, "other">]?.icon} title={`${CATEGORY_INFO[tab as Exclude<Category, "other">]?.label ?? ""}: находок нет`}>
                    {CATEGORY_INFO[tab as Exclude<Category, "other">]?.empty}
                  </EmptyState>
                }
              />
            )
          }
        </QueryState>
      )}
      <FindingDialog finding={open} all={history.data ?? all} onClose={() => setOpen(null)} />
    </div>
  );
}
