import { useQuery } from "@tanstack/react-query";
import { CircleSlash, Coins, Download, FileText, FlaskConical, GitFork, ListTree, ListChecks, Play, ShieldAlert, ThumbsDown, ThumbsUp } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { TERMINAL, api, download, errorText, type RunDetail } from "./api";
import { queryClient, useFindings, useProject, useRun } from "./data/queries";
import { href } from "./shell/routes";
import {
  Badge,
  Button,
  Card,
  DataTable,
  duration,
  EmptyState,
  ErrorState,
  formatCost,
  formatNumber,
  GateBadge,
  PageHeader,
  Progress,
  QueryState,
  relativeTime,
  SeverityBadge,
  Skeleton,
  StateBadge,
  Stat,
  Tabs,
  toast,
} from "./ui";
import "./pages/tasks.css";

// One run of the orchestrator: state, approvals, steps and their DAG, Quality Gates,
// findings, artifacts and worker jobs. Polls while the run is active (useRun).

const RunGraph = lazy(() => import("./pages/RunGraph"));

const TASK_TONE: Record<string, "success" | "danger" | "accent" | "neutral" | "warning"> = {
  SUCCEEDED: "success",
  FAILED: "danger",
  RUNNING: "accent",
  PENDING: "neutral",
  SKIPPED: "neutral",
  CANCELLED: "warning",
};
const TASK_LABEL: Record<string, string> = {
  SUCCEEDED: "Готово",
  FAILED: "Ошибка",
  RUNNING: "Выполняется",
  PENDING: "Ожидает",
  SKIPPED: "Пропущен",
  CANCELLED: "Отменён",
};

function TaskBadge({ status }: { status: string }) {
  return (
    <Badge tone={TASK_TONE[status] ?? "neutral"} dot>
      {TASK_LABEL[status] ?? status}
    </Badge>
  );
}

function Approvals({ run }: { run: RunDetail }) {
  const pending = run.approvals.filter((a) => a.status === "pending");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"yes" | "no" | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (pending.length === 0) return null;
  function decide(approve: boolean) {
    setBusy(approve ? "yes" : "no");
    setError(null);
    api.decide(run.id, approve, note).then(
      () => {
        setBusy(null);
        setNote("");
        void queryClient.invalidateQueries({ queryKey: ["run", run.id] });
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: approve ? "success" : "warning", title: approve ? "Одобрено" : "Отклонено" });
      },
      (err: unknown) => {
        setBusy(null);
        setError(errorText(err));
      },
    );
  }
  return (
    <Card className="approval-card" title="Ждёт вашего согласия" icon={<ShieldAlert size={15} />}>
      <ul className="approval-list">
        {pending.map((a) => (
          <li key={a.id}>
            <code>{a.operation}</code>
            {a.reason && <span className="text-secondary"> — {a.reason}</span>}
          </li>
        ))}
      </ul>
      <div className="approval-actions">
        <input className="input grow" name="note" aria-label="Комментарий к решению" placeholder="Комментарий (необязательно)" value={note} onChange={(e) => setNote(e.target.value)} />
        <Button variant="primary" icon={<ThumbsUp size={14} />} loading={busy === "yes"} disabled={busy !== null} onClick={() => decide(true)}>
          Одобрить
        </Button>
        <Button variant="danger" icon={<ThumbsDown size={14} />} loading={busy === "no"} disabled={busy !== null} onClick={() => decide(false)}>
          Отклонить
        </Button>
      </div>
      {error && <p className="error">{error}</p>}
    </Card>
  );
}

function Overview({ run }: { run: RunDetail }) {
  const counts = run.tasks.reduce<Record<string, number>>((acc, t) => ({ ...acc, [t.status]: (acc[t.status] ?? 0) + 1 }), {});
  const done = (counts.SUCCEEDED ?? 0) + (counts.SKIPPED ?? 0);
  return (
    <div className="run-overview">
      <Card title="Шаги" icon={<ListChecks size={15} />}>
        <div className="stack">
          <Progress value={done} max={run.tasks.length || 1} tone={counts.FAILED ? "danger" : "success"} label="Выполнено шагов" />
          <div className="stats">
            <Stat label="всего" value={run.tasks.length} />
            <Stat label="готово" value={counts.SUCCEEDED ?? 0} tone={counts.SUCCEEDED ? "success" : undefined} />
            <Stat label="в работе" value={counts.RUNNING ?? 0} />
            <Stat label="с ошибкой" value={counts.FAILED ?? 0} tone={counts.FAILED ? "danger" : undefined} />
          </div>
        </div>
      </Card>
      <Card title="Ресурсы моделей" icon={<Coins size={15} />}>
        {run.usage && run.usage.input_tokens + run.usage.output_tokens > 0 ? (
          <div className="stats">
            <Stat label="токены на входе" value={formatNumber(run.usage.input_tokens)} />
            <Stat label="токены на выходе" value={formatNumber(run.usage.output_tokens)} />
            <Stat label="стоимость" value={formatCost(run.usage.cost_usd)} />
          </div>
        ) : (
          <p className="text-muted tile-empty">Этот run не обращался к моделям.</p>
        )}
      </Card>
      <Card className="span-wide" title="Quality Gates" icon={<FlaskConical size={15} />}>
        {run.gates && run.gates.length > 0 ? (
          <ul className="gate-list">
            {run.gates.map((g) => (
              <li key={g.gate}>
                <span className="gate-list-name">
                  <code>{g.gate}</code>
                  {g.blocking && <Badge>блокирующий</Badge>}
                </span>
                <GateBadge result={g.result} />
                <span className="gate-list-reason">{g.reason ?? ""}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted tile-empty">
            {TERMINAL.has(run.state) ? "У этого run нет шага Quality Gates." : "Quality Gates появятся, когда run дойдёт до проверки."}
          </p>
        )}
      </Card>
    </div>
  );
}

function Steps({ run }: { run: RunDetail }) {
  if (run.tasks.length === 0) return <EmptyState title="Шагов пока нет" />;
  return (
    <ol className="steps">
      {run.tasks.map((t) => (
        <li key={t.key} className={`step status-${t.status.toLowerCase()}`}>
          <span className="step-dot" aria-hidden />
          <div className="step-body">
            <div className="step-head">
              <code className="step-key">{t.key}</code>
              <TaskBadge status={t.status} />
              <span className="text-muted step-meta">
                {t.phase}
                {t.attempts > 1 ? ` · попыток: ${t.attempts}` : ""}
              </span>
            </div>
            {t.error && <pre className="step-error">{t.error}</pre>}
          </div>
        </li>
      ))}
    </ol>
  );
}

function Findings({ runId }: { runId: string }) {
  const findings = useFindings({ run_id: runId });
  return (
    <QueryState query={findings}>
      {(rows) => (
        <DataTable
          label="Находки run"
          rows={rows}
          rowKey={(f) => f.id}
          initialSort={{ id: "severity" }}
          empty={<EmptyState title="Находок нет" icon={<ShieldAlert size={20} />}>Анализаторы этого run ничего не нашли или ещё не запускались.</EmptyState>}
          columns={[
            { id: "severity", header: "Важность", cell: (f) => <SeverityBadge severity={f.severity} />, sort: (f) => ["critical", "high", "medium", "low", "info"].indexOf(f.severity.toLowerCase()), width: "110px" },
            { id: "title", header: "Находка", cell: (f) => <span className="cell-main">{f.title}</span>, sort: (f) => f.title },
            { id: "file", header: "Файл", cell: (f) => (f.file_path ? <span className="path">{f.file_path}{f.line ? `:${f.line}` : ""}</span> : <span className="text-muted">—</span>), sort: (f) => f.file_path ?? "" },
            { id: "source", header: "Инструмент", cell: (f) => <Badge>{f.source}</Badge>, sort: (f) => f.source },
          ]}
        />
      )}
    </QueryState>
  );
}

function Artifacts({ runId, state }: { runId: string; state: string }) {
  const artifacts = useQuery({ queryKey: ["artifacts", runId, state], queryFn: () => api.artifacts(runId) });
  return (
    <QueryState query={artifacts}>
      {(list) =>
        list.length === 0 ? (
          <EmptyState title="Артефактов нет" icon={<FileText size={20} />} />
        ) : (
          <ul className="artifact-list">
            {list.map((a) => (
              <li key={a.id}>
                <FileText size={15} aria-hidden className="text-muted" />
                <span className="artifact-name truncate">{a.name}</span>
                <span className="text-muted artifact-meta">
                  {a.media_type} · {formatNumber(a.size_bytes)} Б
                </span>
                <Button size="sm" icon={<Download size={13} />} onClick={() => void download(`/runs/${runId}/artifacts/${a.id}`, a.name)}>
                  Скачать
                </Button>
              </li>
            ))}
          </ul>
        )
      }
    </QueryState>
  );
}

export function RunPage({ id }: { id: string }) {
  const run = useRun(id);
  const project = useProject(run.data?.project_id);
  // Who started it and when: the run detail does not carry it, the project's run list does.
  const projectId = run.data?.project_id;
  const siblings = useQuery({
    queryKey: ["runs", { project_id: projectId }],
    queryFn: () => api.allRuns({ limit: 100, project_id: projectId }),
    enabled: Boolean(projectId),
  });
  const [tab, setTab] = useState("overview");
  const [cancelling, setCancelling] = useState(false);

  if (run.isLoading) return <Skeleton lines={6} />;
  if (run.error || !run.data) return <ErrorState error={run.error ?? "run not found"} onRetry={() => void run.refetch()} title="Run не загрузился" />;
  const r = run.data;
  const summary = siblings.data?.find((s) => s.id === r.id);
  const active = !TERMINAL.has(r.state);
  const pending = r.approvals.filter((a) => a.status === "pending").length;

  function cancel() {
    setCancelling(true);
    api.cancel(id).then(
      () => {
        setCancelling(false);
        void queryClient.invalidateQueries({ queryKey: ["run", id] });
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: "warning", title: "Run отменён" });
      },
      (err: unknown) => {
        setCancelling(false);
        toast({ tone: "danger", title: "Не удалось отменить", text: errorText(err) });
      },
    );
  }

  return (
    <div className="page run-page">
      <PageHeader
        icon={<Play size={18} />}
        title={r.goal || r.kind || "Run"}
        subtitle={
          <span className="run-meta">
            <StateBadge state={r.state} />
            {r.kind && <Badge tone="accent">{r.kind}</Badge>}
            <a href={href("project", r.project_id)}>{project.data?.name ?? "проект"}</a>
            {summary && (
              <>
                <span>· {summary.requested_by}</span>
                <span>· начат {relativeTime(summary.created_at)}</span>
                <span>· {duration(summary.created_at, summary.finished_at)}</span>
              </>
            )}
            <code className="text-muted">{r.id}</code>
          </span>
        }
        actions={
          active && (
            <Button variant="danger" icon={<CircleSlash size={14} />} loading={cancelling} onClick={cancel}>
              Отменить
            </Button>
          )
        }
      />
      {r.error && (
        <div className="state-error run-error" role="alert">
          <ShieldAlert size={16} aria-hidden />
          <pre>{r.error}</pre>
        </div>
      )}
      <Approvals run={r} />
      <Tabs
        label="Разделы run"
        value={tab}
        onChange={setTab}
        items={[
          { id: "overview", label: "Обзор" },
          { id: "steps", label: "Шаги", count: r.tasks.length, icon: <ListTree size={13} /> },
          { id: "graph", label: "Граф", icon: <GitFork size={13} /> },
          { id: "findings", label: "Находки" },
          { id: "artifacts", label: "Артефакты" },
          { id: "jobs", label: "Задания", count: r.jobs.length },
        ]}
      />
      {tab === "overview" && <Overview run={r} />}
      {tab === "steps" && <Steps run={r} />}
      {tab === "graph" &&
        (r.tasks.length === 0 ? (
          <EmptyState title="Шагов пока нет" />
        ) : (
          <Suspense fallback={<Skeleton height={420} />}>
            <RunGraph tasks={r.tasks} dependencies={r.dependencies ?? []} />
          </Suspense>
        ))}
      {tab === "findings" && <Findings runId={id} />}
      {tab === "artifacts" && <Artifacts runId={id} state={r.state} />}
      {tab === "jobs" && (
        <DataTable
          label="Задания worker'ов"
          rows={r.jobs.map((j, i) => ({ ...j, key: `${j.kind}-${i}` }))}
          rowKey={(j) => j.key}
          empty={<EmptyState title="Заданий нет" />}
          columns={[
            { id: "kind", header: "Задание", cell: (j) => <code>{j.kind}</code> },
            { id: "state", header: "Состояние", cell: (j) => <Badge tone={j.state === "failed" ? "danger" : j.state === "done" ? "success" : "neutral"}>{j.state}</Badge> },
            { id: "attempts", header: "Попытки", cell: (j) => j.attempts },
            { id: "error", header: "Ошибка", cell: (j) => (j.error ? <span className="path">{j.error}</span> : <span className="text-muted">—</span>) },
          ]}
        />
      )}
      {pending > 0 && tab !== "overview" && <p className="text-muted">Есть {pending} ожидающих согласия — решение выше.</p>}
    </div>
  );
}
