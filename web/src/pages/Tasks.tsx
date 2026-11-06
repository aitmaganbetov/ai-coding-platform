import { useQueries } from "@tanstack/react-query";
import { ListChecks, Plus, ShieldAlert, ThumbsDown, ThumbsUp } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { api, errorText, type GlobalRun } from "../api";
import { queryClient, useProjects, useRuns } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Dialog,
  duration,
  EmptyState,
  isActiveRun,
  PageHeader,
  QueryState,
  relativeTime,
  SearchInput,
  Segmented,
  Select,
  Skeleton,
  StateBadge,
  Tabs,
  toast,
} from "../ui";
import "./tasks.css";

// All runs the user may see, across projects: filters, the approval queue (people decide,
// here or on the run page) and starting a new orchestrator task.

type TabId = "all" | "active" | "approvals" | "done" | "failed";
const KINDS = ["audit", "discover", "develop", "maintain"] as const;
type Kind = (typeof KINDS)[number];

const KIND_TEXT: Record<Kind, string> = {
  audit: "Аудит: анализаторы в песочнице, находки и Quality Gates. Файлы не меняются.",
  discover: "Обзор: конфигурация, git, стек и контекст проекта. Без сети и без изменений.",
  develop: "Разработка агентами оркестратора: ветка, тесты, ревью. Нужен ИИ-провайдер проекта.",
  maintain: "Сопровождение: план исправлений → согласие → одно проверенное исправление. Нужен ИИ-провайдер.",
};

function tabFrom(query: Record<string, string>): TabId {
  if (["all", "active", "approvals", "done", "failed"].includes(query.tab ?? "")) return query.tab as TabId;
  if (query.state === "active") return "active";
  if (query.state === "AWAITING_APPROVAL") return "approvals";
  if (query.state === "FAILED") return "failed";
  if (query.state === "COMPLETED") return "done";
  return "all";
}

const IN_TAB: Record<TabId, (r: GlobalRun) => boolean> = {
  all: () => true,
  active: (r) => isActiveRun(r.state),
  approvals: (r) => r.state === "AWAITING_APPROVAL",
  done: (r) => r.state === "COMPLETED",
  failed: (r) => r.state === "FAILED" || r.state === "CANCELLED",
};

function ApprovalQueue({ runs }: { runs: GlobalRun[] }) {
  const details = useQueries({ queries: runs.map((r) => ({ queryKey: ["run", r.id], queryFn: () => api.run(r.id) })) });
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  if (runs.length === 0)
    return (
      <EmptyState icon={<ShieldAlert size={20} />} title="Ничего не ждёт согласия">
        Run'ы, которым нужно ваше решение (изменение критичных файлов, миграции, merge), появятся здесь.
      </EmptyState>
    );
  function decide(run: GlobalRun, approve: boolean) {
    setBusy(`${run.id}:${approve}`);
    setErrors((e) => ({ ...e, [run.id]: "" }));
    api.decide(run.id, approve, notes[run.id] ?? "").then(
      () => {
        setBusy(null);
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        void queryClient.invalidateQueries({ queryKey: ["run", run.id] });
        toast({ tone: approve ? "success" : "warning", title: approve ? "Одобрено" : "Отклонено", text: run.goal || run.kind || run.id });
      },
      (err: unknown) => {
        setBusy(null);
        setErrors((e) => ({ ...e, [run.id]: errorText(err) }));
      },
    );
  }
  return (
    <div className="approval-queue">
      {runs.map((r, i) => {
        const pending = details[i]?.data?.approvals.filter((a) => a.status === "pending") ?? [];
        return (
          <Card key={r.id} className="approval-card" title={<a href={href("run", r.id)}>{r.goal || r.kind}</a>} icon={<ShieldAlert size={15} />} actions={<Badge>{r.project_name}</Badge>}>
            {details[i]?.isLoading ? (
              <Skeleton lines={1} />
            ) : pending.length > 0 ? (
              <ul className="approval-list">
                {pending.map((a) => (
                  <li key={a.id}>
                    <code>{a.operation}</code>
                    {a.reason && <span className="text-secondary"> — {a.reason}</span>}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted tile-empty">Подробности — на странице run.</p>
            )}
            <div className="approval-actions">
              <input
                className="input grow"
                aria-label={`Комментарий: ${r.goal || r.kind}`}
                placeholder="Комментарий (необязательно)"
                value={notes[r.id] ?? ""}
                onChange={(e) => setNotes((n) => ({ ...n, [r.id]: e.target.value }))}
              />
              <Button variant="primary" icon={<ThumbsUp size={14} />} loading={busy === `${r.id}:true`} disabled={busy !== null} onClick={() => decide(r, true)}>
                Одобрить
              </Button>
              <Button variant="danger" icon={<ThumbsDown size={14} />} loading={busy === `${r.id}:false`} disabled={busy !== null} onClick={() => decide(r, false)}>
                Отклонить
              </Button>
            </div>
            {errors[r.id] && <p className="error">{errors[r.id]}</p>}
          </Card>
        );
      })}
    </div>
  );
}

/** "title: body" per line → the issues a maintain run takes ({id, title, body}). */
export function parseIssues(text: string): { id: string; title: string; body: string }[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, i) => {
      const at = line.indexOf(":");
      const title = (at > 0 ? line.slice(0, at) : line).trim();
      const body = at > 0 ? line.slice(at + 1).trim() : "";
      return { id: `I-${i + 1}`, title, body };
    });
}

function NewTask({ open, onOpenChange, defaultProject }: { open: boolean; onOpenChange: (open: boolean) => void; defaultProject: string }) {
  const projects = useProjects();
  const [project, setProject] = useState(defaultProject);
  const [kind, setKind] = useState<Kind>("audit");
  const [tools, setTools] = useState("");
  const [strict, setStrict] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [base, setBase] = useState("");
  const [issues, setIssues] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setError(null);
      setProject((p) => p || defaultProject || projects.data?.[0]?.id || "");
    }
  }, [open, defaultProject, projects.data]);

  function params(): Record<string, unknown> {
    if (kind === "audit") {
      const list = tools.split(/[\s,]+/).filter(Boolean);
      return { ...(list.length ? { tools: list } : {}), ...(strict ? { strict: true } : {}) };
    }
    if (kind === "develop") return { title: title.trim(), ...(body.trim() ? { body: body.trim() } : {}), ...(base.trim() ? { base: base.trim() } : {}) };
    if (kind === "maintain") return { issues: parseIssues(issues), ...(base.trim() ? { base: base.trim() } : {}) };
    return {};
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!project) return;
    setBusy(true);
    setError(null);
    api.createRun(project, kind, params()).then(
      (run) => {
        setBusy(false);
        onOpenChange(false);
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: "success", title: "Задача поставлена", text: kind });
        window.location.hash = href("run", run.id);
      },
      (err: unknown) => {
        setBusy(false);
        setError(errorText(err));
      },
    );
  }

  const invalid = !project || (kind === "develop" && !title.trim()) || (kind === "maintain" && parseIssues(issues).length === 0);
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Новая задача оркестратору"
      description="Run выполняют worker'ы платформы: со своими агентами, песочницей и Quality Gates."
      wide
    >
      <form className="task-form" onSubmit={submit}>
        <Select
          label="Проект"
          value={project}
          onChange={setProject}
          options={[{ value: "", label: "— выберите проект —" }, ...(projects.data ?? []).map((p) => ({ value: p.id, label: p.name }))]}
        />
        <div className="task-form-kind">
          <span className="field-label">Вид задачи</span>
          <Segmented label="Вид задачи" value={kind} onChange={setKind} options={KINDS.map((k) => ({ value: k, label: k }))} />
          <p className="text-muted hint">{KIND_TEXT[kind]}</p>
        </div>
        {kind === "audit" && (
          <>
            <label>
              Анализаторы (необязательно)
              <input className="input" value={tools} onChange={(e) => setTools(e.target.value)} placeholder="semgrep, gitleaks, trivy — пусто: все для стека" />
            </label>
            <label className="check">
              <input type="checkbox" checked={strict} onChange={(e) => setStrict(e.target.checked)} /> Строго: пропущенный блокирующий gate считать провалом
            </label>
          </>
        )}
        {kind === "develop" && (
          <>
            <label>
              Задача
              <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Например: добавить экспорт отчёта в CSV" required />
            </label>
            <label>
              Описание и критерии приёмки
              <textarea rows={5} value={body} onChange={(e) => setBody(e.target.value)} />
            </label>
          </>
        )}
        {kind === "maintain" && (
          <label>
            Проблемы — по одной в строке: «заголовок: описание»
            <textarea rows={5} value={issues} onChange={(e) => setIssues(e.target.value)} placeholder="Падает экспорт: 500 при пустом списке" />
          </label>
        )}
        {(kind === "develop" || kind === "maintain") && (
          <label>
            Базовая ветка (необязательно)
            <input className="input" value={base} onChange={(e) => setBase(e.target.value)} placeholder="ветка по умолчанию проекта" />
          </label>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-foot task-form-foot">
          <Button onClick={() => onOpenChange(false)}>Отмена</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={invalid}>
            Запустить
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

export default function Tasks({ query }: { query: Record<string, string> }) {
  const projects = useProjects();
  const currentProject = useUi((s) => s.projectId);
  const [tab, setTab] = useState<TabId>(() => tabFrom(query));
  const [project, setProject] = useState(query.project ?? "");
  const [kind, setKind] = useState(query.kind ?? "");
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  useEffect(() => setTab(tabFrom(query)), [query.tab, query.state]);
  useEffect(() => setProject(query.project ?? ""), [query.project]);

  const runs = useRuns({ project_id: project || undefined, kind: kind || undefined });
  const all = runs.data ?? [];
  const words = search.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = (r: GlobalRun) =>
    words.every((w) => `${r.goal} ${r.kind ?? ""} ${r.project_name} ${r.requested_by} ${r.id}`.toLowerCase().includes(w));
  const shown = useMemo(() => all.filter((r) => IN_TAB[tab](r) && matches(r)), [all, tab, search]);
  const count = (id: TabId) => all.filter(IN_TAB[id]).length;

  function changeTab(id: string) {
    setTab(id as TabId);
    window.history.replaceState(null, "", href("tasks", undefined, { tab: id === "all" ? undefined : id, project: project || undefined }));
  }

  return (
    <div className="page">
      <PageHeader
        icon={<ListChecks size={18} />}
        title="Задачи"
        subtitle="Run'ы оркестратора во всех ваших проектах: состояние, согласования, результаты."
        actions={
          <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreating(true)}>
            Новая задача
          </Button>
        }
      />
      <Tabs
        label="Отбор задач"
        value={tab}
        onChange={changeTab}
        items={[
          { id: "all", label: "Все", count: all.length },
          { id: "active", label: "Активные", count: count("active") },
          { id: "approvals", label: "Ждут согласия", count: count("approvals") },
          { id: "done", label: "Завершённые", count: count("done") },
          { id: "failed", label: "С ошибкой", count: count("failed") },
        ]}
      />
      <div className="filters">
        <SearchInput label="Поиск задач" value={search} onChange={setSearch} placeholder="Цель, вид, проект, автор, id" />
        <Select
          label="Проект"
          hideLabel
          value={project}
          onChange={setProject}
          options={[{ value: "", label: "Все проекты" }, ...(projects.data ?? []).map((p) => ({ value: p.id, label: p.name }))]}
        />
        <Select label="Вид" hideLabel value={kind} onChange={setKind} options={[{ value: "", label: "Все виды" }, ...KINDS.map((k) => ({ value: k, label: k }))]} />
      </div>
      <QueryState query={runs} skeleton={<Skeleton lines={6} />}>
        {() =>
          tab === "approvals" ? (
            <ApprovalQueue runs={shown} />
          ) : (
            <DataTable
              label="Задачи"
              rows={shown}
              rowKey={(r) => r.id}
              onRowClick={(r) => (window.location.hash = href("run", r.id))}
              initialSort={{ id: "started", desc: true }}
              empty={
                <EmptyState
                  icon={<ListChecks size={20} />}
                  title={all.length === 0 ? "Задач пока нет" : "Ничего не найдено"}
                  action={
                    all.length === 0 && (
                      <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreating(true)}>
                        Новая задача
                      </Button>
                    )
                  }
                >
                  {all.length === 0 ? "Запустите аудит, обзор или задачу агентам оркестратора." : "Измените фильтры или строку поиска."}
                </EmptyState>
              }
              columns={[
                { id: "state", header: "Состояние", cell: (r) => <StateBadge state={r.state} />, sort: (r) => r.state, width: "150px" },
                {
                  id: "goal",
                  header: "Задача",
                  cell: (r) => (
                    <>
                      <span className="cell-main">{r.goal || r.kind}</span>
                      <span className="cell-sub">
                        {r.kind} · <code>{r.id.slice(-8)}</code>
                      </span>
                    </>
                  ),
                  sort: (r) => r.goal,
                },
                { id: "project", header: "Проект", cell: (r) => r.project_name, sort: (r) => r.project_name },
                { id: "by", header: "Автор", cell: (r) => r.requested_by, sort: (r) => r.requested_by },
                { id: "started", header: "Начат", cell: (r) => <span title={r.created_at}>{relativeTime(r.created_at)}</span>, sort: (r) => r.created_at },
                { id: "duration", header: "Длительность", cell: (r) => <span className="mono">{duration(r.created_at, r.finished_at)}</span>, sort: (r) => (r.finished_at ? new Date(r.finished_at).getTime() : Date.now()) - new Date(r.created_at).getTime() },
              ]}
            />
          )
        }
      </QueryState>
      <NewTask open={creating} onOpenChange={setCreating} defaultProject={project || currentProject || ""} />
    </div>
  );
}
