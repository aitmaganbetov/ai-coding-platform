import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Check, FolderPlus, GitBranch, Radar, Sparkles, Wand2 } from "lucide-react";
import { useState } from "react";
import { api, errorText, getToken } from "../api";
import { type Workflow, WORKFLOWS } from "../ChatPanel";
import { useProject, useRun } from "../data/queries";
import { NewProjectForm } from "../NewProject";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import { Badge, Card, cx, ErrorState, formatNumber, isActiveRun, LinkButton, PageHeader, Skeleton, StateBadge } from "../ui";
import "./projects.css";

// Onboarding of a project: Connect → Scan (a real discovery run) → Understand (its report)
// → Select a mode → Execute (in the AI Workspace, where the agent plans and the user agrees).

const STEPS = [
  { id: "connect", label: "Подключение", text: "Папка, Git-репозиторий или каталог на сервере" },
  { id: "scan", label: "Сканирование", text: "Discovery-запуск оркестратора" },
  { id: "understand", label: "Анализ", text: "Стек, git и состав проекта из отчёта" },
  { id: "mode", label: "Режим", text: "Создание, разработка, модернизация, аудит или сопровождение" },
  { id: "execute", label: "Выполнение", text: "План, ваше согласие и работа агента в AI Workspace" },
] as const;

type Section = Record<string, unknown>;
interface DiscoveryReport {
  sections: Record<string, Section>;
}

/** The discovery report the run saved (an artifact): facts, not guesses. */
async function loadReport(runId: string): Promise<DiscoveryReport | null> {
  const artifacts = await api.artifacts(runId);
  const report = artifacts.find((a) => a.name === "discovery-report.json");
  if (!report) return null;
  const token = getToken();
  const response = await fetch(`/api/v1/runs/${runId}/artifacts/${report.id}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: "omit",
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as DiscoveryReport;
}

const list = (value: unknown): string[] => (Array.isArray(value) ? value.map(String) : []);

function Stepper({ current }: { current: number }) {
  return (
    <ol className="stepper" aria-label="Шаги подключения">
      {STEPS.map((s, i) => (
        <li key={s.id} className={cx(i < current && "done", i === current && "current")} aria-current={i === current ? "step" : undefined}>
          <span className="step-mark">{i < current ? <Check size={12} aria-hidden /> : i + 1}</span>
          <span className="step-text">
            <strong>{s.label}</strong>
            <small>{s.text}</small>
          </span>
        </li>
      ))}
    </ol>
  );
}

function Facts({ report }: { report: DiscoveryReport }) {
  const stacks = report.sections["discover.stacks"] ?? {};
  const git = report.sections["discover.git"] ?? {};
  const context = report.sections["discover.context"] ?? {};
  const detected = list(stacks.detected);
  const missing = list(stacks.missing_in_config);
  const extensions = Object.entries((context.top_extensions as Record<string, number> | undefined) ?? {});
  return (
    <dl className="kv">
      <dt>Обнаруженный стек</dt>
      <dd>
        {detected.length ? (
          <span className="project-stacks">
            {detected.map((s) => (
              <span key={s} className="stack-chip">
                {s}
              </span>
            ))}
          </span>
        ) : (
          "не определён"
        )}
        {missing.length > 0 && <span className="cell-sub">Нет в конфигурации: {missing.join(", ")}</span>}
      </dd>
      <dt>Git</dt>
      <dd>
        {git.is_repository ? (
          <span className="row">
            <GitBranch size={13} aria-hidden /> {String(git.branch ?? "—")}
            {git.head ? <code>{String(git.head).slice(0, 8)}</code> : null}
            <Badge tone={git.clean ? "success" : "warning"}>{git.clean ? "чисто" : `изменений: ${String(git.changed_files ?? "?")}`}</Badge>
          </span>
        ) : (
          <span className="text-muted">{String(git.reason ?? "не репозиторий")}</span>
        )}
      </dd>
      <dt>Файлы в индексе</dt>
      <dd>
        {typeof context.indexed_files === "number" ? (
          <>
            {formatNumber(context.indexed_files)} файлов
            {typeof context.indexed_bytes === "number" ? ` · ${formatNumber(Math.round(context.indexed_bytes / 1024))} КБ` : ""}
            {extensions.length > 0 && (
              <span className="cell-sub">
                {extensions
                  .slice(0, 6)
                  .map(([ext, n]) => `${ext} ${n}`)
                  .join(" · ")}
              </span>
            )}
          </>
        ) : (
          "—"
        )}
      </dd>
    </dl>
  );
}

function Scan({ projectId, runId, scanError }: { projectId: string; runId: string | null; scanError: string | null }) {
  const run = useRun(runId);
  const finished = run.data && !isActiveRun(run.data.state);
  const report = useQuery({
    queryKey: ["discovery-report", runId],
    queryFn: () => loadReport(runId ?? ""),
    enabled: Boolean(runId && run.data?.state === "COMPLETED"),
    staleTime: Infinity,
  });
  const project = useProject(projectId);
  const configured = project.data?.config?.stacks ?? [];
  return (
    <div className="onboard-grid">
      <Card title="Сканирование" icon={<Radar size={15} />}>
        {scanError ? (
          <ErrorState title="Discovery не запущен" error={scanError} />
        ) : !runId || run.isLoading ? (
          <Skeleton lines={3} />
        ) : run.data ? (
          <div className="stack">
            <div className="row">
              <StateBadge state={run.data.state} />
              <a href={href("run", run.data.id)} className="tile-link">
                run {run.data.id.slice(-8)}
              </a>
            </div>
            <ul className="scan-steps">
              {run.data.tasks.map((t) => (
                <li key={t.key} className={t.status.toLowerCase()}>
                  <code>{t.key}</code>
                  <Badge tone={t.status === "SUCCEEDED" ? "success" : t.status === "FAILED" ? "danger" : "neutral"}>{t.status}</Badge>
                </li>
              ))}
            </ul>
            {run.data.error && <p className="error">{run.data.error}</p>}
          </div>
        ) : (
          <ErrorState error={run.error} />
        )}
      </Card>
      <Card title="Что нашла платформа" icon={<Wand2 size={15} />}>
        {!finished ? (
          <p className="text-muted">{scanError ? "Без сканирования фактов нет: режим можно выбрать и так." : "Появится, когда сканирование завершится."}</p>
        ) : report.isLoading ? (
          <Skeleton lines={3} />
        ) : report.data ? (
          <Facts report={report.data} />
        ) : (
          <p className="text-muted">
            Отчёт discovery недоступен{report.error ? ` (${errorText(report.error)})` : ""}. Подробности — на{" "}
            <a href={href("run", runId ?? "")}>странице запуска</a>.
          </p>
        )}
        {configured.length > 0 && (
          <p className="cell-sub">Стек в конфигурации проекта: {configured.join(", ")}</p>
        )}
      </Card>
    </div>
  );
}

function Modes({ projectId }: { projectId: string }) {
  return (
    <Card title="Выберите режим работы" icon={<Sparkles size={15} />}>
      <div className="mode-grid">
        {(Object.keys(WORKFLOWS) as Workflow[]).map((w) => (
          <a key={w} className="mode-card" href={href("ai", projectId, { mode: w })}>
            <strong>{WORKFLOWS[w].label}</strong>
            <span>{WORKFLOWS[w].hint}</span>
            <span className="mode-go">
              Открыть AI Workspace <ArrowRight size={13} aria-hidden />
            </span>
          </a>
        ))}
      </div>
      <p className="text-muted">
        Агент Claude Code или Codex работает в вашей рабочей области по процессу режима: сначала план, ваше согласие, затем изменения
        и проверка аудитом платформы.
      </p>
    </Card>
  );
}

/** Connect a project: a folder from the computer, a Git repository or a server directory. */
export function NewProjectPage() {
  const setCurrent = useUi((s) => s.setProject);
  const [project, setProject] = useState<{ id: string; name: string } | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [scanError, setScanError] = useState<string | null>(null);
  const run = useRun(runId);
  const scanned = scanError !== null || (run.data !== undefined && !isActiveRun(run.data.state));
  const step = !project ? 0 : scanned ? 3 : 1;

  function created(p: { id: string; name: string }) {
    setProject(p);
    setCurrent(p.id);
    api.createRun(p.id, "discover", {}).then(
      (r) => setRunId(r.id),
      (err: unknown) => setScanError(errorText(err)),
    );
  }

  return (
    <div className="page">
      <PageHeader
        icon={<FolderPlus size={18} />}
        title={project ? `Проект «${project.name}» подключён` : "Новый проект"}
        subtitle={
          project
            ? "Платформа изучает проект. Затем выберите режим — работа продолжится в AI Workspace."
            : "Подключите папку с компьютера, Git-репозиторий (в том числе приватный) или каталог на сервере. Затем платформа определит стек, и можно выбрать режим работы."
        }
        actions={
          project && (
            <LinkButton href={href("project", project.id)} icon={<ArrowRight size={14} />}>
              Обзор проекта
            </LinkButton>
          )
        }
      />
      <Stepper current={step} />
      {!project ? (
        <NewProjectForm onCancel={() => (window.location.hash = href("projects"))} onCreated={created} />
      ) : (
        <div className="stack">
          <Scan projectId={project.id} runId={runId} scanError={scanError} />
          <Modes projectId={project.id} />
        </div>
      )}
    </div>
  );
}
