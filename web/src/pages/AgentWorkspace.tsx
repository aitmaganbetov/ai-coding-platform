import { useQuery } from "@tanstack/react-query";
import { Bot, GitBranch, GitPullRequest, Play, RotateCcw, Send, ShieldAlert, Square, Wand2 } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { type AgentExecution, api, artifactText, errorText, type RunDetail } from "../api";
import { useAI } from "../data/ai";
import { queryClient, useRun, useRuns } from "../data/queries";
import { href } from "../shell/routes";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  formatCost,
  formatNumber,
  isActiveRun,
  LinkButton,
  QueryState,
  relativeTime,
  Segmented,
  Select,
  Skeleton,
  StateBadge,
  toast,
  type Tone,
} from "../ui";
import { parseIssues } from "./Tasks";

// The agents' workspace: a task in plain words goes to the orchestrator's agents (a
// develop or maintain run on the server, with the project's AI provider), and the run is
// followed here: its steps and the agents behind them, the approvals it waits for, what
// the agents produced (plan, changes, tests, review, diff) and a follow-up ("доработать")
// that continues from the result branch.

export type WorkKind = "develop" | "maintain";
export const WORK_KINDS: Record<WorkKind, { label: string; placeholder: string; steps: string }> = {
  develop: {
    label: "Разработка",
    placeholder:
      "Что сделать? Первая строка — заголовок задачи, дальше — подробности и критерии приёмки.\nНапример: Экспорт отчёта в CSV\nКнопка «Скачать CSV» на странице отчёта, колонки как в таблице, тесты на формат.",
    steps:
      "Developer пишет код и тесты в песочнице → Reviewer проверяет изменения (до N кругов) → тесты проекта в песочнице → коммит в рабочую ветку (и pull request, если подключён GitHub).",
  },
  maintain: {
    label: "Исправления",
    placeholder:
      "Что исправить или обновить? Каждая строка — отдельная проблема: «заголовок: подробности».\nНапример: Ошибка округления налога: total() теряет копейки на суммах > 1000",
    steps:
      "Maintenance составляет план исправлений → вы утверждаете план → Developer и Reviewer делают одно проверенное исправление с регрессионным тестом.",
  },
};

// Handlers that run catalogue agents inside one step: the engine records the handler,
// the work is the agents'.
export const HANDLER_AGENTS: Record<string, string[]> = {
  "builtin.develop.implement": ["developer", "reviewer"],
  "builtin.maintain.item": ["developer", "reviewer"],
};

/** The catalogue agents behind a recorded handler (``agent.security.triage`` → security). */
export function agentsOfHandler(handler: string): string[] {
  const mapped = HANDLER_AGENTS[handler];
  if (mapped) return mapped;
  const match = /^agent\.([a-z]+)/.exec(handler);
  return match?.[1] ? [match[1]] : [];
}

const STEPS: Record<string, { label: string; agents?: string[] }> = {
  "discover.git": { label: "Состояние репозитория" },
  "develop.implement": { label: "Реализация: код, тесты, ревью", agents: ["developer", "reviewer"] },
  "develop.verify": { label: "Проверка: тесты проекта в песочнице" },
  "git.push": { label: "Публикация рабочей ветки" },
  "github.pr": { label: "Pull request" },
  "github.pr.verify": { label: "Проверка pull request" },
  "maintain.baseline": { label: "Исходное состояние: тесты до изменений" },
  "maintain.plan": { label: "План исправлений", agents: ["maintenance"] },
  "maintain.approve": { label: "Согласование плана" },
  "maintain.deps": { label: "Обновление зависимостей" },
  "maintain.item": { label: "Исправление с регрессионным тестом", agents: ["developer", "reviewer"] },
  "maintain.verify": { label: "Проверка исправления" },
};

const OPERATIONS: Record<string, string> = {
  external_llm: "Отправить код и контекст проекта внешней модели",
  pr_create: "Создать pull request",
  dependency_update: "Обновить зависимости",
  migration: "Изменить миграции базы данных",
  critical_file_change: "Изменить критичный файл",
  delete: "Удалить файлы",
  merge: "Слияние",
  deploy: "Деплой",
  host_command: "Команда на хосте",
  spec_approval: "Утвердить требования",
};

const TASK_STATUS: Record<string, { label: string; tone: Tone }> = {
  SUCCEEDED: { label: "готово", tone: "success" },
  FAILED: { label: "ошибка", tone: "danger" },
  RUNNING: { label: "выполняется", tone: "accent" },
  PENDING: { label: "ждёт", tone: "neutral" },
  READY: { label: "в очереди", tone: "neutral" },
  SKIPPED: { label: "пропущен", tone: "neutral" },
  CANCELLED: { label: "отменён", tone: "neutral" },
  WAITING_APPROVAL: { label: "ждёт согласия", tone: "warning" },
};

const EXEC_STATUS: Record<string, { label: string; tone: Tone }> = {
  running: { label: "работает", tone: "accent" },
  succeeded: { label: "успешно", tone: "success" },
  failed: { label: "ошибка", tone: "danger" },
  budget: { label: "превышен бюджет", tone: "danger" },
  needs_approval: { label: "ждёт согласия", tone: "warning" },
  cancelled: { label: "отменено", tone: "neutral" },
};

const capital = (name: string) => name.charAt(0).toUpperCase() + name.slice(1);
const firstLine = (text: string) => text.trim().split("\n")[0]?.trim() ?? "";

/** The run's parameters for a task written in plain words. */
export function workParams(kind: WorkKind, prompt: string, base: string, iterations: number): Record<string, unknown> {
  const text = prompt.trim();
  const branch = base.trim() ? { base: base.trim() } : {};
  if (kind === "develop") {
    const title = firstLine(text).slice(0, 200);
    const body = text.length > title.length ? text : "";
    return { title, ...(body ? { body } : {}), max_iterations: iterations, ...branch };
  }
  return { issues: parseIssues(text), ...branch };
}

// --------------------------------------------------------------- composer --
function AIStatus({ projectId }: { projectId: string }) {
  const ai = useAI(projectId);
  if (ai.isLoading || ai.error || !ai.data) return null;
  const { effective, key, valid, error } = ai.data;
  const external = effective.provider === "openai" || effective.provider === "anthropic";
  const problem = !effective.enabled || effective.provider === "none"
    ? "ИИ-провайдер проекта не включён: агентам нечем думать."
    : external && !key.set
      ? `Нет ключа ${effective.provider}: задайте его в «Моделях».`
      : !valid
        ? error ?? "Настройки ИИ проекта не прошли проверку."
        : null;
  if (problem)
    return (
      <div className="agent-ws-warn" role="alert">
        <ShieldAlert size={15} aria-hidden />
        <div>
          <p>{problem}</p>
          <LinkButton href={href("models")} size="sm">
            Настроить модели
          </LinkButton>
        </div>
      </div>
    );
  return (
    <p className="text-muted agent-ws-model">
      Модель агентов: <b>{effective.provider}</b>
      {effective.model ? ` · ${effective.model}` : ""}
      {external && !effective.private ? " · перед первым обращением run может попросить согласие отправить код провайдеру" : ""}
    </p>
  );
}

/** What the run panel puts in the box: a follow-up (from the result branch) or a retry. */
export interface Seed {
  prompt: string;
  base: string;
  n: number;
}

function Composer({ projectId, seed, onStarted }: { projectId: string; seed: Seed; onStarted: (runId: string) => void }) {
  const [kind, setKind] = useState<WorkKind>("develop");
  const [prompt, setPrompt] = useState(seed.prompt);
  const [base, setBase] = useState(seed.base);
  const [iterations, setIterations] = useState("3");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const params = workParams(kind, prompt, base, Number(iterations));
  const empty = kind === "develop" ? !firstLine(prompt) : parseIssues(prompt).length === 0;

  function start() {
    setStarting(true);
    setError(null);
    api.createRun(projectId, kind, params).then(
      (run) => {
        setStarting(false);
        setPrompt("");
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: "success", title: "Агенты получили задачу", text: firstLine(prompt) });
        onStarted(run.id);
      },
      (err: unknown) => {
        setStarting(false);
        setError(errorText(err));
      },
    );
  }

  return (
    <Card title="Задача агентам" icon={<Wand2 size={15} />}>
      <div className="stack">
        <AIStatus projectId={projectId} />
        <Segmented
          label="Вид задачи"
          value={kind}
          onChange={setKind}
          options={(Object.keys(WORK_KINDS) as WorkKind[]).map((k) => ({ value: k, label: WORK_KINDS[k].label }))}
        />
        <label className="agent-ws-field">
          <span>Что сделать</span>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={WORK_KINDS[kind].placeholder}
            rows={7}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !empty && !starting) start();
            }}
          />
        </label>
        <div className="agent-ws-options">
          <label className="agent-ws-field">
            <span>Базовая ветка</span>
            <input value={base} onChange={(e) => setBase(e.target.value)} placeholder="основная ветка проекта" />
          </label>
          {kind === "develop" && (
            <Select
              label="Кругов Developer ↔ Reviewer"
              value={iterations}
              onChange={setIterations}
              options={["1", "2", "3", "4", "5"].map((n) => ({ value: n, label: n }))}
            />
          )}
        </div>
        <p className="text-muted hint">{WORK_KINDS[kind].steps}</p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <Button variant="primary" icon={<Play size={14} />} onClick={start} loading={starting} disabled={empty}>
          Запустить агентов
        </Button>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------- list --
function TaskList({ projectId, selected, onSelect }: { projectId: string; selected: string | null; onSelect: (id: string) => void }) {
  const runs = useRuns({ project_id: projectId });
  const work = (runs.data ?? []).filter((r) => r.kind === "develop" || r.kind === "maintain");
  return (
    <Card title="Задачи агентов" icon={<Bot size={15} />}>
      <QueryState query={runs} skeleton={<Skeleton lines={3} />}>
        {() =>
          work.length === 0 ? (
            <p className="text-muted tile-empty">Агенты ещё не получали задач в этом проекте.</p>
          ) : (
            <ul className="agent-ws-list" aria-label="Задачи агентов">
              {work.map((r) => (
                <li key={r.id}>
                  <button type="button" aria-current={r.id === selected ? "true" : undefined} className={r.id === selected ? "active" : undefined} onClick={() => onSelect(r.id)}>
                    <StateBadge state={r.state} />
                    <span className="truncate">{r.goal || r.kind}</span>
                    <small className="text-muted">
                      {r.kind === "develop" ? "разработка" : "исправления"} · {relativeTime(r.created_at)}
                    </small>
                  </button>
                </li>
              ))}
            </ul>
          )
        }
      </QueryState>
    </Card>
  );
}

// -------------------------------------------------------------------- run --
interface Implementation {
  summary?: string;
  plan?: unknown;
  changes?: { path?: string; op?: string; action?: string }[];
  tests?: unknown;
  review?: { verdict?: string; findings?: unknown[]; summary?: string } | null;
  iterations?: number;
}

function useArtifacts(run: RunDetail | undefined) {
  const id = run?.id;
  const done = run ? !isActiveRun(run.state) : false;
  const list = useQuery({ queryKey: ["artifacts", id, run?.state], queryFn: () => api.artifacts(id as string), enabled: Boolean(id) });
  const find = (name: string) => (list.data ?? []).find((a) => a.name === name);
  const implementation = find("implementation.json");
  const diff = find("change.diff");
  const report = useQuery({
    queryKey: ["artifact", id, implementation?.id],
    queryFn: async () => JSON.parse(await artifactText(id as string, implementation?.id as string)) as Implementation,
    enabled: Boolean(id && implementation),
    staleTime: done ? Infinity : 0,
  });
  const patch = useQuery({
    queryKey: ["artifact", id, diff?.id],
    queryFn: () => artifactText(id as string, diff?.id as string),
    enabled: Boolean(id && diff),
    staleTime: done ? Infinity : 0,
  });
  return { report: report.data, diff: patch.data };
}

function planLines(plan: unknown): string[] {
  if (!plan) return [];
  if (typeof plan === "string") return plan.split("\n").filter(Boolean);
  if (Array.isArray(plan)) return plan.map((p) => (typeof p === "string" ? p : JSON.stringify(p)));
  if (typeof plan === "object") {
    const steps = (plan as { steps?: unknown }).steps;
    if (Array.isArray(steps)) return planLines(steps);
  }
  return [JSON.stringify(plan)];
}

function DiffView({ text }: { text: string }) {
  return (
    <pre className="agent-ws-diff">
      {text.split("\n").map((line, i) => (
        <span key={i} className={line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : undefined}>
          {line}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

function Result({ run, onFollowUp }: { run: RunDetail; onFollowUp: (prompt: string, base: string) => void }) {
  const { report, diff } = useArtifacts(run);
  const implement = run.results?.["develop.implement"] ?? run.results?.["maintain.item"];
  const pr = run.results?.["github.pr"];
  const branch = typeof implement?.branch === "string" ? implement.branch : null;
  const [feedback, setFeedback] = useState("");
  if (!implement && !report && !diff) return null;
  const plan = planLines(report?.plan);
  const changes = report?.changes ?? [];
  return (
    <Card title="Результат агентов" icon={<GitBranch size={15} />}>
      <div className="stack">
        {(branch || pr) && (
          <p className="agent-ws-where">
            {branch && (
              <>
                Ветка <code>{branch}</code>
                {implement?.commit ? (
                  <>
                    {" "}
                    · коммит <code>{String(implement.commit).slice(0, 10)}</code>
                  </>
                ) : null}
              </>
            )}
            {typeof pr?.url === "string" && (
              <a href={pr.url} target="_blank" rel="noreferrer" className="agent-ws-pr">
                <GitPullRequest size={14} aria-hidden /> Pull request{pr.number ? ` #${pr.number}` : ""}
              </a>
            )}
          </p>
        )}
        {(report?.summary || implement?.summary) && <p className="agent-ws-summary">{report?.summary ?? String(implement?.summary)}</p>}
        <div className="agent-ws-facts">
          {(report?.iterations ?? implement?.iterations) !== undefined && <Badge>кругов Developer ↔ Reviewer: {report?.iterations ?? implement?.iterations}</Badge>}
          {report?.review?.verdict && <Badge tone={/approve|pass|ok/i.test(report.review.verdict) ? "success" : "warning"}>ревью: {report.review.verdict}</Badge>}
          {Array.isArray(report?.review?.findings) && <Badge>замечаний ревьюера: {report?.review?.findings.length}</Badge>}
          {changes.length > 0 && <Badge>файлов: {changes.length}</Badge>}
        </div>
        {plan.length > 0 && (
          <details className="agent-ws-details">
            <summary>План ({plan.length})</summary>
            <ol>
              {plan.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ol>
          </details>
        )}
        {changes.length > 0 && (
          <details className="agent-ws-details">
            <summary>Изменённые файлы ({changes.length})</summary>
            <ul>
              {changes.map((c, i) => (
                <li key={i}>
                  <code>{c.path ?? JSON.stringify(c)}</code> {c.op ?? c.action ? <span className="text-muted">{c.op ?? c.action}</span> : null}
                </li>
              ))}
            </ul>
          </details>
        )}
        {diff && (
          <details className="agent-ws-details">
            <summary>Diff</summary>
            <DiffView text={diff} />
          </details>
        )}
        {run.state === "COMPLETED" && branch && (
          <div className="agent-ws-follow">
            <label className="agent-ws-field">
              <span>Доработать результат</span>
              <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} rows={3} placeholder="Что изменить или добавить? Агенты продолжат с этой ветки." />
            </label>
            <Button
              icon={<Send size={14} />}
              disabled={!feedback.trim()}
              onClick={() => {
                onFollowUp(`Доработка: ${firstLine(feedback)}\nПродолжи задачу «${run.goal}» в ветке ${branch}.\n${feedback.trim()}`, branch);
                setFeedback("");
              }}
            >
              Доработать
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

function Approvals({ run }: { run: RunDetail }) {
  const pending = run.approvals.filter((a) => a.status === "pending");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  if (pending.length === 0) return null;
  function decide(approve: boolean) {
    setBusy(true);
    api.decide(run.id, approve, note).then(
      () => {
        setBusy(false);
        setNote("");
        void queryClient.invalidateQueries({ queryKey: ["run", run.id] });
        toast({ tone: approve ? "success" : "warning", title: approve ? "Разрешено — агенты продолжают" : "Отклонено" });
      },
      (err: unknown) => {
        setBusy(false);
        toast({ tone: "danger", title: "Решение не принято", text: errorText(err) });
      },
    );
  }
  return (
    <div className="agent-ws-approval" role="group" aria-label="Агенты ждут вашего решения">
      <ShieldAlert size={16} aria-hidden />
      <div className="stack">
        <b>Агенты ждут вашего решения</b>
        {pending.map((a) => (
          <p key={a.id}>
            {OPERATIONS[a.operation] ?? a.operation}
            {a.reason ? <span className="text-muted"> — {a.reason}</span> : null}
          </p>
        ))}
        <textarea aria-label="Комментарий к решению" value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder="Комментарий (необязательно)" />
        <div className="row">
          <Button variant="primary" onClick={() => decide(true)} loading={busy}>
            Разрешить
          </Button>
          <Button variant="danger" onClick={() => decide(false)} disabled={busy}>
            Отклонить
          </Button>
        </div>
      </div>
    </div>
  );
}

function Steps({ run }: { run: RunDetail }) {
  return (
    <ol className="agent-ws-steps" aria-label="Шаги">
      {run.tasks.map((t) => {
        const info = STEPS[t.key];
        const status = TASK_STATUS[t.status] ?? { label: t.status.toLowerCase(), tone: "neutral" as Tone };
        return (
          <li key={t.key} className={`step-${t.status.toLowerCase()}`}>
            <span className="agent-ws-step-dot" aria-hidden />
            <div className="agent-ws-step-body">
              <div className="agent-ws-step-head">
                <span>{info?.label ?? t.key}</span>
                <Badge tone={status.tone}>{status.label}</Badge>
              </div>
              <small className="text-muted">
                <code>{t.key}</code>
                {info?.agents ? ` · ${info.agents.map(capital).join(" ↔ ")}` : ""}
                {t.attempts > 1 ? ` · попыток: ${t.attempts}` : ""}
              </small>
              {t.error && <pre className="step-error">{t.error}</pre>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function AgentsOfRun({ executions }: { executions: AgentExecution[] }) {
  if (executions.length === 0) return <p className="text-muted">Агенты этого run'а ещё не начали работу.</p>;
  return (
    <ul className="agent-ws-execs" aria-label="Агенты в этой задаче">
      {executions.map((e) => {
        const agents = agentsOfHandler(e.agent_name);
        const status = EXEC_STATUS[e.status] ?? { label: e.status, tone: "neutral" as Tone };
        return (
          <li key={e.id}>
            <span className="agent-ws-exec-name" title={e.agent_name}>
              {agents.length ? agents.map(capital).join(" ↔ ") : (STEPS[e.task_key]?.label ?? e.agent_name)}
            </span>
            <Badge tone={status.tone}>{status.label}</Badge>
            <code className="text-muted truncate">{e.task_key}</code>
            <span className="text-muted mono">
              {e.duration_seconds !== null ? `${Math.round(e.duration_seconds)} с` : "…"} · {formatNumber(e.input_tokens + e.output_tokens)} ток. · {formatCost(e.cost_usd)}
            </span>
            {e.error && <span className="agent-exec-error" title={e.error}>{e.error.split("\n")[0]}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function RunPanel({ runId, projectId, onFollowUp, onRetry }: { runId: string; projectId: string; onFollowUp: (prompt: string, base: string) => void; onRetry: (prompt: string) => void }) {
  const run = useRun(runId);
  const active = run.data ? isActiveRun(run.data.state) : true;
  const executions = useQuery({
    queryKey: ["agent-executions", projectId, 500],
    queryFn: () => api.agentExecutions(projectId, 500),
    refetchInterval: active ? 3000 : false,
  });
  const mine = useMemo(
    () => (executions.data ?? []).filter((e) => e.run_id === runId).sort((a, b) => a.started_at.localeCompare(b.started_at)),
    [executions.data, runId],
  );
  const [cancelling, setCancelling] = useState(false);
  return (
    <QueryState query={run} skeleton={<Skeleton lines={8} />}>
      {(r) => (
        <div className="stack">
          <div className="agent-ws-run-head">
            <div>
              <h3>{r.goal || r.kind}</h3>
              <small className="text-muted">
                {r.kind === "develop" ? "Разработка" : r.kind === "maintain" ? "Исправления" : r.kind} · run <code>{r.id.slice(-8)}</code>
                {r.usage && r.usage.input_tokens + r.usage.output_tokens > 0 ? ` · ${formatNumber(r.usage.input_tokens + r.usage.output_tokens)} ток. · ${formatCost(r.usage.cost_usd)}` : ""}
              </small>
            </div>
            <div className="row">
              <StateBadge state={r.state} />
              {isActiveRun(r.state) && (
                <Button
                  size="sm"
                  icon={<Square size={12} />}
                  loading={cancelling}
                  onClick={() => {
                    setCancelling(true);
                    api.cancel(r.id).finally(() => {
                      setCancelling(false);
                      void queryClient.invalidateQueries({ queryKey: ["run", r.id] });
                    });
                  }}
                >
                  Остановить
                </Button>
              )}
              <LinkButton href={href("run", r.id)} size="sm">
                Открыть run
              </LinkButton>
            </div>
          </div>
          {r.error && (
            <div className="agent-ws-error" role="alert">
              <pre className="step-error">{r.error}</pre>
              {(r.state === "FAILED" || r.state === "CANCELLED") && (
                <Button size="sm" icon={<RotateCcw size={13} />} onClick={() => onRetry(r.goal.replace(/^(develop|maintain):\s*/, ""))}>
                  Уточнить и запустить заново
                </Button>
              )}
            </div>
          )}
          <Approvals run={r} />
          <section className="agent-ws-section" aria-label="Ход работы">
            <h4>Ход работы</h4>
            <Steps run={r} />
          </section>
          <section className="agent-ws-section" aria-label="Агенты">
            <h4>Агенты</h4>
            <AgentsOfRun executions={mine} />
          </section>
          <Result run={r} onFollowUp={onFollowUp} />
        </div>
      )}
    </QueryState>
  );
}

// ------------------------------------------------------------------- page --
export default function AgentWorkspace({ projectId, initialRun }: { projectId: string; initialRun?: string }) {
  const [selected, setSelected] = useState<string | null>(initialRun ?? null);
  const main = useRef<HTMLDivElement>(null);
  function open(runId: string) {
    setSelected(runId);
    // One column (narrow screens): the run is below the form and the list.
    if (window.matchMedia?.("(max-width: 960px)").matches) main.current?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }
  // A new seed re-creates the composer with its text and base branch, ready to check and send.
  const [seed, setSeed] = useState<Seed>({ prompt: "", base: "", n: 0 });
  return (
    <div className="agent-ws">
      <div className="agent-ws-side">
        <Composer key={seed.n} projectId={projectId} seed={seed} onStarted={open} />
        <TaskList projectId={projectId} selected={selected} onSelect={open} />
      </div>
      <div className="agent-ws-main" ref={main}>
        {selected ? (
          <RunPanel
            runId={selected}
            projectId={projectId}
            onFollowUp={(prompt, branch) => setSeed((s) => ({ prompt, base: branch, n: s.n + 1 }))}
            onRetry={(prompt) => setSeed((s) => ({ prompt: `${prompt}\n`, base: "", n: s.n + 1 }))}
          />
        ) : (
          <WorkspaceIntro />
        )}
      </div>
    </div>
  );
}

function WorkspaceIntro() {
  return (
    <EmptyState icon={<Bot size={20} />} title="Дайте агентам задачу">
      Опишите задачу слева своими словами и нажмите «Запустить агентов». Оркестратор поставит её агентам проекта: Developer напишет код и тесты в
      песочнице, Reviewer проверит, платформа прогонит тесты и закоммитит результат в рабочую ветку. Здесь будет видно, что делает каждый агент,
      о чём он спрашивает разрешения и что получилось; результат можно доработать новой задачей.
    </EmptyState>
  );
}
