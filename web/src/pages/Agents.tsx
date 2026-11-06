import { Bot, Cpu, GitFork, LayoutGrid, Wrench } from "lucide-react";
import { lazy, Suspense, useMemo, useState } from "react";
import type { AgentExecution, AgentInfo, GlobalRun } from "../api";
import { useAgentExecutions, useAgents, useProject, useRun, useRuns } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Card,
  EmptyState,
  formatCost,
  formatNumber,
  isActiveRun,
  PageHeader,
  QueryState,
  relativeTime,
  RUN_STATES,
  Segmented,
  Select,
  Skeleton,
  type Tone,
} from "../ui";
import type { StepAnnotation } from "./RunGraph";
import "./tasks.css";

// Agent Control Center: the twelve agents of the orchestrator (ТЗ §6) with what they are
// doing in the current project, and the workflow of a run — which step each agent took
// and where it failed. Everything is read from the agents' recorded executions.

const RunGraph = lazy(() => import("./RunGraph"));

export type AgentStatus = "working" | "waiting" | "failed" | "idle";
const STATUS: Record<AgentStatus, { label: string; tone: Tone }> = {
  working: { label: "В работе", tone: "accent" },
  waiting: { label: "Ждёт согласия", tone: "warning" },
  failed: { label: "Ошибка", tone: "danger" },
  idle: { label: "Свободен", tone: "neutral" },
};
const FAILED = new Set(["failed", "budget"]);

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
          <strong>{agent.name}</strong>
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
                    <code className="truncate">{shortHandler(name)}</code>
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

export default function Agents() {
  const projectId = useUi((s) => s.projectId);
  const project = useProject(projectId);
  const executions = useAgentExecutions(projectId);
  const runs = useRuns(projectId ? { project_id: projectId } : {});
  const [view, setView] = useState<"grid" | "flow">("grid");
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
