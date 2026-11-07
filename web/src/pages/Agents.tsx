import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Bot, Cpu, GitFork, LayoutGrid, ListChecks, Wrench } from "lucide-react";
import { lazy, Suspense, useMemo, useState } from "react";
import { type AgentExecution, type AgentInfo, api, type GlobalRun } from "../api";
import { useAgentExecutions, useAgents, useProject, useRun, useRuns } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Card,
  type Column,
  DataTable,
  EmptyState,
  formatCost,
  formatNumber,
  isActiveRun,
  LinkButton,
  PageHeader,
  QueryState,
  relativeTime,
  RUN_STATES,
  Segmented,
  Select,
  shortId,
  Skeleton,
  Stat,
  StateBadge,
  type Tone,
} from "../ui";
import type { StepAnnotation } from "./RunGraph";
import "./tasks.css";

// Agent Control Center: the twelve agents of the orchestrator (ТЗ §6) with what they are
// doing in the current project, and the workflow of a run — which step each agent took
// and where it failed. A card opens the agent (#/agents?agent=<name>): every task it ran
// in the project with its status, run, time, tokens, cost and error. Everything is read
// from the agents' recorded executions.

const RunGraph = lazy(() => import("./RunGraph"));

export type AgentStatus = "working" | "waiting" | "failed" | "idle";
const STATUS: Record<AgentStatus, { label: string; tone: Tone }> = {
  working: { label: "В работе", tone: "accent" },
  waiting: { label: "Ждёт согласия", tone: "warning" },
  failed: { label: "Ошибка", tone: "danger" },
  idle: { label: "Свободен", tone: "neutral" },
};
const FAILED = new Set(["failed", "budget"]);
// The outcome of one attempt (AgentExecution.status, written by the engine).
const EXEC_STATUS: Record<string, { label: string; tone: Tone }> = {
  running: { label: "выполняется", tone: "accent" },
  succeeded: { label: "успешно", tone: "success" },
  failed: { label: "ошибка", tone: "danger" },
  budget: { label: "превышен бюджет", tone: "danger" },
  needs_approval: { label: "ждёт согласия", tone: "warning" },
  cancelled: { label: "отменено", tone: "neutral" },
};
export const execLabel = (status: string) => EXEC_STATUS[status]?.label ?? status;
const DETAIL_LIMIT = 500; // the API's maximum: an agent's whole recent history

/** Executions of a catalogue agent (its handler is ``agent.<name>``). */
export function executionsOf(name: string, executions: AgentExecution[]): AgentExecution[] {
  return executions
    .filter((e) => e.agent_name === `agent.${name}` || e.agent_name === name)
    .sort((a, b) => b.started_at.localeCompare(a.started_at));
}

/** What an agent does now, from its latest execution and the state of that run. */
export function agentStatus(executions: AgentExecution[], runState: (runId: string) => string | undefined): AgentStatus {
  const latest = executions[0];
  if (!latest) return "idle";
  const state = runState(latest.run_id);
  if (latest.status === "needs_approval" || (state === "AWAITING_APPROVAL" && !latest.finished_at)) return "waiting";
  if (!latest.finished_at && latest.status === "running" && (state === undefined || isActiveRun(state))) return "working";
  if (FAILED.has(latest.status)) return "failed";
  return "idle";
}

function shortHandler(name: string): string {
  return name.startsWith("agent.") ? name.slice(6) : name;
}

function AgentCard({ agent, executions, runState }: { agent: AgentInfo; executions: AgentExecution[]; runState: (id: string) => string | undefined }) {
  const status = agentStatus(executions, runState);
  const latest = executions[0];
  const tokens = executions.reduce((n, e) => n + e.input_tokens + e.output_tokens, 0);
  const cost = executions.reduce((n, e) => n + e.cost_usd, 0);
  const models = [...new Set(executions.flatMap((e) => e.models))];
  return (
    <article className={`agent-card status-${status}`} aria-label={`Агент ${agent.name}`}>
      <header className="agent-card-head">
        <span className="agent-avatar" aria-hidden>
          <Bot size={16} />
        </span>
        <div className="agent-title">
          <strong>
            <a className="agent-link" href={href("agents", undefined, { agent: agent.name })}>
              {agent.name}
            </a>
          </strong>
          <small className="truncate" title={`${agent.inputs} → ${agent.outputs}`}>
            {agent.outputs}
          </small>
        </div>
        <Badge tone={STATUS[status].tone} dot>
          {STATUS[status].label}
        </Badge>
      </header>
      <div className="agent-tags">
        <Badge tone={agent.requires_llm ? "info" : "neutral"}>{agent.requires_llm ? "нужна LLM" : "без LLM"}</Badge>
        {agent.implemented ? <Badge tone="success">реализован</Badge> : <Badge tone="warning">план: {agent.sprint}</Badge>}
        {models.map((m) => (
          <Badge key={m} tone="accent">
            {m}
          </Badge>
        ))}
      </div>
      <p className="agent-io">
        <span className="text-muted">Вход:</span> {agent.inputs}
      </p>
      {latest ? (
        <dl className="kv agent-kv">
          <dt>Задача</dt>
          <dd>
            <a href={href("run", latest.run_id)}>
              <code>{latest.task_key}</code>
            </a>
          </dd>
          <dt>Последний запуск</dt>
          <dd>
            {relativeTime(latest.started_at)}
            {latest.duration_seconds !== null ? ` · ${Math.round(latest.duration_seconds)} с` : " · ещё идёт"}
          </dd>
          <dt>Токены / стоимость</dt>
          <dd className="mono">
            {formatNumber(tokens)} · {formatCost(cost)}
          </dd>
        </dl>
      ) : (
        <p className="text-muted agent-none">В этом проекте ещё не запускался.</p>
      )}
      {latest?.error && <pre className="step-error">{latest.error}</pre>}
      {executions.length > 0 && (
        <ul className="agent-actions" aria-label="Последние действия">
          {executions.slice(0, 3).map((e) => (
            <li key={e.id}>
              <span className={`exec-dot exec-${e.status}`} aria-hidden />
              <code className="truncate">{e.task_key}</code>
              <span className="text-muted">{e.status}</span>
              <span className="text-muted">{relativeTime(e.started_at)}</span>
            </li>
          ))}
        </ul>
      )}
      {(agent.capabilities.length > 0 || agent.allowed_tools.length > 0) && (
        <details className="agent-more">
          <summary>Возможности и инструменты</summary>
          <div className="agent-tags">
            {agent.capabilities.map((c) => (
              <Badge key={`c-${c}`}>{c}</Badge>
            ))}
            {agent.allowed_tools.map((t) => (
              <Badge key={`t-${t}`} tone="info">
                <Wrench size={10} aria-hidden /> {t}
              </Badge>
            ))}
          </div>
        </details>
      )}
    </article>
  );
}

function Grid({ executions, runs }: { executions: AgentExecution[]; runs: GlobalRun[] }) {
  const agents = useAgents();
  const states = useMemo(() => new Map(runs.map((r) => [r.id, r.state])), [runs]);
  const runState = (id: string) => states.get(id);
  const catalogNames = new Set((agents.data ?? []).flatMap((a) => [a.name, `agent.${a.name}`]));
  const others = useMemo(() => {
    const by = new Map<string, AgentExecution[]>();
    for (const e of executions) if (!catalogNames.has(e.agent_name)) by.set(e.agent_name, [...(by.get(e.agent_name) ?? []), e]);
    return [...by.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [executions, agents.data]);
  return (
    <QueryState query={agents} skeleton={<Skeleton lines={6} />} empty={(list) => (list.length === 0 ? <EmptyState title="Каталог агентов пуст" /> : null)}>
      {(list) => (
        <div className="stack">
          <div className="agent-grid">
            {list.map((a) => (
              <AgentCard key={a.name} agent={a} executions={executionsOf(a.name, executions)} runState={runState} />
            ))}
          </div>
          {others.length > 0 && (
            <Card title="Встроенные шаги оркестратора" icon={<Cpu size={15} />}>
              <ul className="handler-list">
                {others.map(([name, list2]) => (
                  <li key={name}>
                    <a className="truncate" href={href("agents", undefined, { agent: name })}>
                      <code>{shortHandler(name)}</code>
                    </a>
                    <span className="text-muted">запусков: {list2.length}</span>
                    <span className={`exec-dot exec-${list2[0]?.status ?? "succeeded"}`} aria-hidden />
                    <span className="text-muted">{list2[0]?.status}</span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      )}
    </QueryState>
  );
}

function Workflow({ executions, runs }: { executions: AgentExecution[]; runs: GlobalRun[] }) {
  const [picked, setPicked] = useState("");
  const runId = picked || runs[0]?.id || "";
  const run = useRun(runId || undefined);
  const annotations = useMemo(() => {
    const out: Record<string, StepAnnotation[]> = {};
    for (const e of executions.filter((x) => x.run_id === runId).sort((a, b) => a.started_at.localeCompare(b.started_at))) {
      out[e.task_key] = [...(out[e.task_key] ?? []), { label: shortHandler(e.agent_name), status: e.status }];
    }
    return out;
  }, [executions, runId]);
  if (runs.length === 0) return <EmptyState icon={<GitFork size={20} />} title="В проекте ещё нет run'ов" />;
  return (
    <div className="stack">
      <div className="filters">
        <Select
          label="Run"
          value={runId}
          onChange={setPicked}
          options={runs.map((r) => ({ value: r.id, label: `${r.goal || r.kind} · ${RUN_STATES[r.state]?.label ?? r.state} · ${relativeTime(r.created_at)}` }))}
        />
        {runId && (
          <a className="tile-link" href={href("run", runId)}>
            Открыть run
          </a>
        )}
      </div>
      <QueryState query={run} skeleton={<Skeleton height={420} />}>
        {(r) =>
          r.tasks.length === 0 ? (
            <EmptyState title="У run ещё нет шагов" />
          ) : (
            <Suspense fallback={<Skeleton height={420} />}>
              <RunGraph tasks={r.tasks} dependencies={r.dependencies ?? []} annotations={annotations} />
            </Suspense>
          )
        }
      </QueryState>
      <p className="text-muted hint">Под каждым шагом — исполнитель (агент или встроенный обработчик) и итог его попытки. Красные связи идут от шага, который упал.</p>
    </div>
  );
}


// ------------------------------------------------------------------ detail --
function seconds(value: number): string {
  const s = Math.round(value);
  return s < 60 ? `${s} с` : `${Math.floor(s / 60)} мин ${s % 60} с`;
}

function durationText(e: AgentExecution): string {
  if (e.duration_seconds === null) return e.finished_at ? "—" : "идёт";
  return seconds(e.duration_seconds);
}

function AgentDetail({ name, projectId, runs }: { name: string; projectId: string; runs: GlobalRun[] }) {
  const agents = useAgents();
  const history = useQuery({
    queryKey: ["agent-executions", projectId, DETAIL_LIMIT],
    queryFn: () => api.agentExecutions(projectId, DETAIL_LIMIT),
  });
  const [status, setStatus] = useState("");
  const info = agents.data?.find((a) => a.name === name || `agent.${a.name}` === name);
  const all = useMemo(() => {
    const list = history.data ?? [];
    return info ? executionsOf(info.name, list) : list.filter((e) => e.agent_name === name).sort((a, b) => b.started_at.localeCompare(a.started_at));
  }, [history.data, info, name]);
  const byRun = useMemo(() => new Map(runs.map((r) => [r.id, r])), [runs]);
  const rows = status ? all.filter((e) => e.status === status) : all;
  const count = (s: string) => all.filter((e) => e.status === s).length;
  const timed = all.filter((e) => e.duration_seconds !== null);
  const average = timed.length ? Math.round(timed.reduce((n, e) => n + (e.duration_seconds ?? 0), 0) / timed.length) : null;
  const models = [...new Set(all.flatMap((e) => e.models))];
  const columns: Column<AgentExecution>[] = [
    { id: "status", header: "Статус", cell: (e) => <Badge tone={EXEC_STATUS[e.status]?.tone ?? "neutral"} dot>{execLabel(e.status)}</Badge>, sort: (e) => e.status, width: "150px" },
    {
      id: "task",
      header: "Задача",
      cell: (e) => {
        const run = byRun.get(e.run_id);
        return (
          <>
            <code className="cell-main">{e.task_key}</code>
            <span className="cell-sub">{run ? run.goal || run.kind || run.mode : `run ${shortId(e.run_id)}`}</span>
            {e.error && (
              <span className="cell-sub agent-exec-error" title={e.error}>
                {e.error.split("\n")[0]}
              </span>
            )}
          </>
        );
      },
      sort: (e) => e.task_key,
    },
    {
      id: "run",
      header: "Run",
      cell: (e) => {
        const run = byRun.get(e.run_id);
        return (
          <a href={href("run", e.run_id)} onClick={(event) => event.stopPropagation()}>
            {run ? <StateBadge state={run.state} /> : <span className="mono">{shortId(e.run_id)}</span>}
          </a>
        );
      },
    },
    { id: "attempt", header: "Попытка", cell: (e) => e.attempt, sort: (e) => e.attempt, width: "84px" },
    { id: "started", header: "Начата", cell: (e) => <span className="text-muted">{relativeTime(e.started_at)}</span>, sort: (e) => e.started_at },
    { id: "duration", header: "Длительность", cell: durationText, sort: (e) => e.duration_seconds ?? -1 },
    { id: "tokens", header: "Токены", cell: (e) => <span className="mono">{formatNumber(e.input_tokens + e.output_tokens)}</span>, sort: (e) => e.input_tokens + e.output_tokens },
    { id: "cost", header: "Стоимость", cell: (e) => <span className="mono">{formatCost(e.cost_usd)}</span>, sort: (e) => e.cost_usd },
    { id: "models", header: "Модели", cell: (e) => (e.models.length ? e.models.join(", ") : "—") },
  ];
  const statuses = [...new Set(all.map((e) => e.status))];
  return (
    <div className="stack">
      <div className="agent-detail-head">
        <LinkButton href={href("agents")} icon={<ArrowLeft size={14} />} size="sm">
          Все агенты
        </LinkButton>
      </div>
      <Card title={info ? `Агент ${info.name}` : shortHandler(name)} icon={<Bot size={15} />}>
        {info ? (
          <div className="stack">
            <p className="agent-io">
              <span className="text-muted">Выход:</span> {info.outputs}
            </p>
            <p className="agent-io">
              <span className="text-muted">Вход:</span> {info.inputs}
            </p>
            <div className="agent-tags">
              <Badge tone={info.requires_llm ? "info" : "neutral"}>{info.requires_llm ? "нужна LLM" : "без LLM"}</Badge>
              {info.implemented ? <Badge tone="success">реализован</Badge> : <Badge tone="warning">план: {info.sprint}</Badge>}
              {info.capabilities.map((c) => (
                <Badge key={`c-${c}`}>{c}</Badge>
              ))}
              {info.allowed_tools.map((t) => (
                <Badge key={`t-${t}`} tone="info">
                  <Wrench size={10} aria-hidden /> {t}
                </Badge>
              ))}
            </div>
          </div>
        ) : (
          <p className="text-muted">Встроенный обработчик оркестратора (шаг без LLM): инструмент, проверка или отчёт.</p>
        )}
      </Card>
      <QueryState query={history} skeleton={<Skeleton lines={6} />}>
        {() =>
          all.length === 0 ? (
            <EmptyState icon={<ListChecks size={20} />} title="В этом проекте ещё не запускался">
              Агенты оркестратора работают в run'ах проекта с его ИИ-провайдером: создайте задачу в «Задачах» или попросите агента в чате передать задачу платформе.
              <div className="agent-detail-actions">
                <LinkButton href={href("tasks")} size="sm">
                  Задачи
                </LinkButton>
              </div>
            </EmptyState>
          ) : (
            <div className="stack">
              <div className="stats" aria-label="Итоги агента">
                <Stat label="запусков" value={all.length} />
                <Stat label="успешно" value={count("succeeded")} tone={count("succeeded") ? "success" : undefined} />
                <Stat label="с ошибкой" value={count("failed") + count("budget")} tone={count("failed") + count("budget") ? "danger" : undefined} />
                <Stat label="в работе" value={count("running")} tone={count("running") ? "accent" : undefined} />
                <Stat label="токенов" value={formatNumber(all.reduce((n, e) => n + e.input_tokens + e.output_tokens, 0))} />
                <Stat label="стоимость" value={formatCost(all.reduce((n, e) => n + e.cost_usd, 0))} />
                <Stat label="среднее время" value={average === null ? "—" : seconds(average)} />
              </div>
              {models.length > 0 && (
                <div className="agent-tags" aria-label="Модели">
                  {models.map((m) => (
                    <Badge key={m} tone="accent">
                      {m}
                    </Badge>
                  ))}
                </div>
              )}
              <div className="filters">
                <Select
                  label="Статус"
                  value={status}
                  onChange={setStatus}
                  options={[{ value: "", label: "Все" }, ...statuses.map((s) => ({ value: s, label: `${execLabel(s)} · ${count(s)}` }))]}
                />
              </div>
              <DataTable
                label="Задачи агента"
                rows={rows}
                columns={columns}
                rowKey={(e) => e.id}
                onRowClick={(e) => (window.location.hash = href("run", e.run_id))}
                initialSort={{ id: "started", desc: true }}
                empty={<EmptyState title="Нет задач с этим статусом" />}
              />
              <p className="text-muted hint">Строка — попытка шага: повтор после ошибки — новая строка. Нажмите, чтобы открыть run.</p>
            </div>
          )
        }
      </QueryState>
    </div>
  );
}

export default function Agents({ query = {} }: { query?: Record<string, string> }) {
  const projectId = useUi((s) => s.projectId);
  const project = useProject(projectId);
  const executions = useAgentExecutions(projectId);
  const runs = useRuns(projectId ? { project_id: projectId } : {});
  const [view, setView] = useState<"grid" | "flow">("grid");
  const opened = query.agent;
  if (opened && projectId)
    return (
      <div className="page">
        <PageHeader
          icon={<Bot size={18} />}
          title={opened.startsWith("agent.") ? opened.slice(6) : opened}
          subtitle={
            <>
              Задачи агента в проекте <strong>{project.data?.name ?? "…"}</strong>: статусы, run'ы, время, токены, стоимость и ошибки.
            </>
          }
        />
        <AgentDetail name={opened} projectId={projectId} runs={runs.data ?? []} />
      </div>
    );
  return (
    <div className="page">
      <PageHeader
        icon={<Bot size={18} />}
        title="Агенты"
        subtitle={
          projectId ? (
            <>
              Агенты оркестратора в проекте <strong>{project.data?.name ?? "…"}</strong>: состояние, задачи, модели и расход.
            </>
          ) : (
            "Агенты оркестратора и их работа в проекте."
          )
        }
        actions={
          <Segmented
            label="Представление"
            value={view}
            onChange={setView}
            options={[
              { value: "grid", label: <><LayoutGrid size={13} aria-hidden /> Сетка</> },
              { value: "flow", label: <><GitFork size={13} aria-hidden /> Поток работы</> },
            ]}
          />
        }
      />
      {!projectId ? (
        <EmptyState icon={<Bot size={20} />} title="Выберите проект">
          Состояние агентов показывается по проекту — выберите его в переключателе слева вверху.
        </EmptyState>
      ) : (
        <QueryState query={executions} skeleton={<Skeleton lines={6} />}>
          {(list) =>
            view === "grid" ? <Grid executions={list} runs={runs.data ?? []} /> : <Workflow executions={list} runs={runs.data ?? []} />
          }
        </QueryState>
      )}
    </div>
  );
}
