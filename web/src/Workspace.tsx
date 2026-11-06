import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bot,
  Download,
  FileCode2,
  FlaskConical,
  GitCompare,
  MessageSquare,
  Package,
  ScrollText,
  ShieldCheck,
  SquareTerminal,
} from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  ApiError,
  api,
  download,
  errorText,
  type FileEntry,
  type FileScope,
  type GitStatus,
  type GlobalRun,
  type ProjectDetail,
  type WorkspaceInfo,
} from "./api";
import { ChatPanel, type Workflow, WORKFLOWS } from "./ChatPanel";
import { queryClient, useAgentExecutions, useRun, useRuns } from "./data/queries";
import { ChangesReview, diffKey, usePendingCount } from "./review/ChangesReview";
import { href } from "./shell/routes";
import {
  Badge,
  Button,
  duration,
  EmptyState,
  formatCost,
  formatNumber,
  GateBadge,
  isActiveRun,
  relativeTime,
  Segmented,
  StateBadge,
  Tabs,
  toast,
} from "./ui";
import "./pages/projects.css";

// Heavy parts load on demand: the editor (CodeMirror) and the terminal (xterm.js).
const CodeEditor = lazy(() => import("./CodeEditor"));
const TerminalPanel = lazy(() => import("./TerminalPanel"));

interface OpenFile {
  path: string;
  content: string | null; // as loaded (the editor's document)
  text: string; // as edited
  version: string | null;
  binary: boolean;
  tooLarge: boolean;
  size: number;
  loadedAt: number;
  dirty: boolean; // edited and not saved yet
}

type CenterTab = "chat" | "code" | "diff" | "tests" | "logs" | "artifacts";
type Pane = "left" | "main" | "right";

/** A column width the user dragged, kept in this browser only (storage may be blocked). */
function useStoredWidth(key: string, initial: number, min: number, max: number) {
  const [width, setWidth] = useState(() => {
    try {
      const saved = Number(window.localStorage.getItem(key));
      return saved >= min && saved <= max ? saved : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (next: number) => {
      const value = Math.round(Math.min(max, Math.max(min, next)));
      setWidth(value);
      try {
        window.localStorage.setItem(key, String(value));
      } catch {
        /* not persisted */
      }
    },
    [key, min, max],
  );
  return [width, set] as const;
}

/** A drag handle between two columns (mouse, touch and arrow keys). */
function Resizer({ label, width, onResize, invert }: { label: string; width: number; onResize: (w: number) => void; invert?: boolean }) {
  // invert: the handle of a right-hand column (dragging left widens it).
  function start(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const origin = event.clientX;
    const from = width;
    const move = (e: PointerEvent) => onResize(from + (invert ? origin - e.clientX : e.clientX - origin));
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.classList.remove("resizing");
    };
    document.body.classList.add("resizing");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }
  function key(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.key === "ArrowRight" ? 16 : event.key === "ArrowLeft" ? -16 : 0;
    if (!step) return;
    event.preventDefault();
    onResize(width + (invert ? -step : step));
  }
  return (
    <div
      className={invert ? "ws-resizer right" : "ws-resizer"}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      tabIndex={0}
      onPointerDown={start}
      onKeyDown={key}
    />
  );
}

const WORKFLOW_IDS = Object.keys(WORKFLOWS) as Workflow[];

// query (from the route): terminal — a command to open the terminal with (from the Copilot);
// draft and mode — a chat message and a platform mode to start an AI task with.
export function Workspace({ id, layout = "code", query = {} }: { id: string; layout?: "ai" | "code"; query?: Record<string, string> }) {
  const initialCommand = query.terminal;
  const initialWorkflow = WORKFLOW_IDS.includes(query.mode as Workflow) ? (query.mode as Workflow) : undefined;
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [info, setInfo] = useState<WorkspaceInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [treeKey, setTreeKey] = useState(0);
  const [open, setOpen] = useState<OpenFile | null>(null);
  // A command handed over by the Copilot (e.g. `claude auth login`) opens the terminal with it.
  const [terminal, setTerminal] = useState(Boolean(initialCommand));
  const [terminalCommand, setTerminalCommand] = useState<string | undefined>(initialCommand);
  const [chat, setChat] = useState(true);
  const [tab, setTab] = useState<CenterTab>(() =>
    (["chat", "code", "diff", "tests", "logs", "artifacts"] as const).find((t) => t === query.tab) ?? "chat",
  );
  // Code Studio: the editor, or the review of the workspace's changes (AI Changes Review).
  const [codeView, setCodeView] = useState<"editor" | "review">(query.view === "review" ? "review" : "editor");
  const pending = usePendingCount(id, Boolean(info?.exists && info.terminal === "on"));
  const [pane, setPane] = useState<Pane>("main");
  const [left, setLeft] = useStoredWidth(`nexus-ws-left-${layout}`, 260, 180, 520);
  const [right, setRight] = useStoredWidth(`nexus-ws-right-${layout}`, layout === "ai" ? 300 : 400, 260, 640);
  const openRef = useRef<OpenFile | null>(null);
  openRef.current = open;

  const load = useCallback(async () => {
    try {
      const [p, w] = await Promise.all([api.project(id), api.workspace(id)]);
      setProject(p);
      setInfo(w);
    } catch (err) {
      setError(errorText(err));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const scope: FileScope = info?.exists ? "workspace" : "project";
  const editable = scope === "workspace";

  async function openWorkspace() {
    setBusy(true);
    setError(null);
    try {
      setInfo(await api.openWorkspace(id));
      setOpen(null);
      setTreeKey((k) => k + 1);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const openFile = useCallback(
    async (path: string) => {
      setError(null);
      try {
        const file = await api.file(id, path, scope);
        setOpen({
          path: file.path,
          content: file.content,
          text: file.content ?? "",
          version: file.version,
          binary: file.binary,
          tooLarge: file.too_large,
          size: file.size,
          loadedAt: Date.now(),
          dirty: false,
        });
        setTab("code");
        setCodeView("editor");
        setPane("main");
      } catch (err) {
        setError(errorText(err));
      }
    },
    [id, scope],
  );

  // The agent changed files: refresh the tree, the review, and the open file unless it has
  // unsaved edits.
  const filesChanged = useCallback(() => {
    setTreeKey((k) => k + 1);
    void queryClient.invalidateQueries({ queryKey: diffKey(id) });
    const current = openRef.current;
    if (current && !current.dirty && !current.binary && !current.tooLarge) void openFile(current.path);
  }, [openFile, id]);

  function openTerminal(command?: string) {
    setTerminalCommand(command);
    setTerminal(true);
  }

  if (!project || !info)
    return error ? (
      <p className="error" role="alert">
        {error}
      </p>
    ) : (
      <p className="muted">Загрузка…</p>
    );
  const agents = info.exists && info.terminal === "on";
  const ai = layout === "ai";

  const tree = (
    <div className="ws-section">
      <div className="ws-section-head">
        <span>Файлы</span>
      </div>
      <ul className="tree" aria-label="Файлы">
        <DirEntries key={`${scope}-${treeKey}`} projectId={id} scope={scope} path="" onOpen={(p) => void openFile(p)} selected={open?.path ?? null} />
      </ul>
      {editable && (
        <NewFile
          projectId={id}
          onCreated={(path) => {
            setTreeKey((k) => k + 1);
            void openFile(path);
          }}
        />
      )}
    </div>
  );

  const editor = open ? (
    <EditorPane
      key={`${open.path}-${open.loadedAt}`}
      projectId={id}
      file={open}
      editable={editable}
      onEdit={(text) => setOpen((f) => (f ? { ...f, text, dirty: true } : f))}
      onSaved={(version, text) => setOpen((f) => (f ? { ...f, version, text, dirty: false } : f))}
      onReload={() => void openFile(open.path)}
      onDeleted={() => {
        setOpen(null);
        setTreeKey((k) => k + 1);
      }}
    />
  ) : (
    <div className="editor-empty">
      <EmptyState icon={<FileCode2 size={20} />} title="Файл не открыт">
        Выберите файл слева.
      </EmptyState>
    </div>
  );

  const review =
    editable && info.terminal === "on" ? (
      <div className="ws-review">
        <ChangesReview projectId={id} onChanged={filesChanged} onOpenFile={(p) => void openFile(p)} />
        <GitPanel projectId={id} prefix={info.branch_prefix} onFilesChanged={filesChanged} commitForm={false} />
      </div>
    ) : (
      <EmptyState icon={<GitCompare size={20} />} title="Изменений нет">
        Изменения появляются в вашей рабочей области: откройте её, чтобы править файлы, проверять изменения агента и фиксировать коммиты.
      </EmptyState>
    );

  const terminalPanel = terminal && (
    <Suspense fallback={<p className="muted">Загрузка терминала…</p>}>
      <TerminalPanel projectId={id} command={terminalCommand} />
    </Suspense>
  );

  const chatPanel = agents ? (
    <ChatPanel
      projectId={id}
      onOpenTerminal={(command) => openTerminal(command)}
      onFilesChanged={filesChanged}
      initialWorkflow={initialWorkflow}
      initialDraft={query.draft}
    />
  ) : (
    <div className="ws-chat-off">
      <EmptyState icon={<Bot size={20} />} title={info.exists ? "Терминалы и агенты выключены на сервере" : "Чату нужна ваша рабочая область"}>
        {!info.git
          ? "Проект не git-репозиторий: доступен только просмотр файлов."
          : info.exists
            ? "Администратор отключил рабочие области (AI_CODING_SERVER_WORKSPACES)."
            : info.can_edit
              ? "Создайте её кнопкой «Открыть мою рабочую область» — Claude Code или Codex будут работать в ней по вашей подписке."
              : "Ваша роль в проекте позволяет только просмотр."}
      </EmptyState>
    </div>
  );

  const paneOptions: { value: Pane; label: string }[] = ai
    ? [
        { value: "left", label: "Файлы" },
        { value: "main", label: "Чат и код" },
        { value: "right", label: "Ход работы" },
      ]
    : [
        { value: "left", label: "Файлы" },
        { value: "main", label: "Редактор" },
        ...(agents && chat ? [{ value: "right" as Pane, label: "Чат" }] : []),
      ];

  const withRight = ai || (agents && chat);
  const gridStyle = { "--ws-left": `${left}px`, "--ws-right": `${right}px` } as CSSProperties;

  return (
    <section className="workspace" data-layout={layout}>
      <header className="ws-header">
        <a className="ws-back" href={href("project", id)}>
          <ArrowLeft size={14} aria-hidden /> {project.name}
        </a>
        <h1 className="ws-title">{ai ? "AI Workspace" : "Code Studio"}</h1>
        <Badge tone={editable ? "accent" : "neutral"}>{editable ? "моя рабочая область" : "файлы проекта, только чтение"}</Badge>
        <span className="card-spacer" />
        <Segmented
          label="Вид рабочей области"
          value={layout}
          onChange={(v) => (window.location.hash = href(v, id))}
          options={[
            { value: "ai", label: "AI Workspace" },
            { value: "code", label: "Code Studio" },
          ]}
        />
        {info.can_edit && info.git && !info.exists && (
          <Button variant="primary" loading={busy} onClick={() => void openWorkspace()}>
            {busy ? "Создание…" : "Открыть мою рабочую область"}
          </Button>
        )}
        {agents && !ai && (
          <Button
            variant={codeView === "review" ? "primary" : "secondary"}
            icon={<GitCompare size={14} />}
            aria-pressed={codeView === "review"}
            onClick={() => setCodeView((v) => (v === "review" ? "editor" : "review"))}
          >
            {codeView === "review" ? "К редактору" : `Изменения${pending ? ` · ${pending}` : ""}`}
          </Button>
        )}
        {agents && !ai && (
          <Button variant={chat ? "secondary" : "primary"} icon={<MessageSquare size={14} />} onClick={() => setChat((c) => !c)}>
            {chat ? "Скрыть чат" : "Чат с ИИ"}
          </Button>
        )}
        {agents && (
          <Button
            icon={<SquareTerminal size={14} />}
            onClick={() => {
              setTerminalCommand(undefined);
              setTerminal((t) => !t);
            }}
          >
            {terminal ? "Скрыть терминал" : "Терминал"}
          </Button>
        )}
      </header>
      {info.git && !info.exists && info.can_edit && (
        <p className="ws-note">
          Рабочая область — ваша собственная копия репозитория проекта: чат с Claude Code и Codex (вход вашим аккаунтом), правка
          файлов, терминал, коммиты. Другие участники её не видят; в проект изменения попадают кнопкой «Отправить в проект» (ветка{" "}
          <code>{info.branch_prefix}…</code>).
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="ws-panes">
        <Segmented label="Панель" value={pane} onChange={setPane} options={paneOptions} />
      </div>
      <div className={`ws-grid ${ai ? "ws-ai" : "ws-code"}${withRight ? " with-right" : ""}`} data-pane={pane} style={gridStyle}>
        <aside className="ws-col ws-left" aria-label={ai ? "Файлы и задачи" : "Файлы и git"}>
          {tree}
          {ai ? <ProjectRuns projectId={id} /> : editable && info.terminal === "on" && <GitPanel projectId={id} prefix={info.branch_prefix} onFilesChanged={() => setTreeKey((k) => k + 1)} />}
        </aside>
        <Resizer label="Ширина левой панели" width={left} onResize={setLeft} />
        <div className="ws-col ws-main">
          {ai ? (
            <>
              <Tabs
                label="Рабочая панель"
                value={tab}
                onChange={(t) => setTab(t as CenterTab)}
                items={[
                  { id: "chat", label: "Chat", icon: <MessageSquare size={13} /> },
                  { id: "code", label: "Code", icon: <FileCode2 size={13} /> },
                  { id: "diff", label: "Diff", icon: <GitCompare size={13} />, count: pending || undefined },
                  { id: "tests", label: "Tests", icon: <FlaskConical size={13} /> },
                  { id: "logs", label: "Logs", icon: <ScrollText size={13} /> },
                  { id: "artifacts", label: "Artifacts", icon: <Package size={13} /> },
                ]}
              />
              {/* The chat stays mounted (its connection and draft survive switching tabs). */}
              <div className="ws-tab-panel ws-chat-panel" hidden={tab !== "chat"}>
                {chatPanel}
              </div>
              {tab === "code" && <div className="ws-tab-panel">{editor}</div>}
              {tab === "diff" && <div className="ws-tab-panel">{review}</div>}
              {tab === "tests" && <TestsTab projectId={id} />}
              {tab === "logs" && <LogsTab projectId={id} />}
              {tab === "artifacts" && <ArtifactsTab projectId={id} />}
            </>
          ) : codeView === "review" ? (
            <div className="ws-tab-panel">{review}</div>
          ) : (
            editor
          )}
          {terminalPanel}
        </div>
        {withRight && <Resizer label="Ширина правой панели" width={right} onResize={setRight} invert />}
        {ai ? (
          <aside className="ws-col ws-right" aria-label="Ход работы">
            <Progress projectId={id} />
          </aside>
        ) : (
          agents &&
          chat && (
            <aside className="ws-col ws-right" aria-label="Чат с ИИ">
              {chatPanel}
            </aside>
          )
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------- platform runs of the project --
function useProjectRuns(projectId: string) {
  return useRuns({ project_id: projectId });
}

function latest(runs: GlobalRun[] | undefined, kind?: string): GlobalRun | undefined {
  const list = (runs ?? []).filter((r) => !kind || r.kind === kind);
  return list.find((r) => r.state === "COMPLETED") ?? list[0];
}

function ProjectRuns({ projectId }: { projectId: string }) {
  const runs = useProjectRuns(projectId);
  return (
    <div className="ws-section">
      <div className="ws-section-head">
        <span>Задачи проекта</span>
        <a href={href("tasks", undefined, { project: projectId })} className="tile-link">
          все
        </a>
      </div>
      {runs.isLoading ? (
        <p className="muted ws-quiet">Загрузка…</p>
      ) : runs.error ? (
        <p className="muted ws-quiet">Список задач недоступен.</p>
      ) : (runs.data ?? []).length === 0 ? (
        <p className="muted ws-quiet">Задач оркестратора пока нет.</p>
      ) : (
        <ul className="ws-runs">
          {(runs.data ?? []).slice(0, 8).map((r) => (
            <li key={r.id}>
              <a href={href("run", r.id)} className="ws-run">
                <StateBadge state={r.state} />
                <span className="truncate">{r.goal || r.kind}</span>
                <small>{relativeTime(r.created_at)}</small>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Progress({ projectId }: { projectId: string }) {
  const runs = useProjectRuns(projectId);
  const executions = useAgentExecutions(projectId);
  const active = (runs.data ?? []).filter((r) => isActiveRun(r.state));
  return (
    <>
      <div className="ws-section">
        <div className="ws-section-head">
          <span>В работе</span>
          {active.length > 0 && <Badge tone="accent">{active.length}</Badge>}
        </div>
        {runs.error ? (
          <p className="muted ws-quiet">Нет данных о задачах.</p>
        ) : active.length === 0 ? (
          <p className="muted ws-quiet">{runs.isLoading ? "Загрузка…" : "Оркестратор сейчас ничего не выполняет для проекта."}</p>
        ) : (
          <ul className="ws-runs">
            {active.map((r) => (
              <li key={r.id}>
                <a href={href("run", r.id)} className="ws-run">
                  <StateBadge state={r.state} />
                  <span className="truncate">{r.goal || r.kind}</span>
                  <small>{duration(r.created_at, null)}</small>
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="ws-section">
        <div className="ws-section-head">
          <span>Агенты оркестратора</span>
          <a href={href("agents")} className="tile-link">
            все
          </a>
        </div>
        {executions.error ? (
          <p className="muted ws-quiet">Нет данных об агентах.</p>
        ) : (executions.data ?? []).length === 0 ? (
          <p className="muted ws-quiet">
            {executions.isLoading ? "Загрузка…" : "Агенты оркестратора ещё не работали в проекте. Чат Claude Code / Codex идёт по вашей подписке отдельно."}
          </p>
        ) : (
          <ul className="ws-agents">
            {(executions.data ?? []).slice(0, 8).map((e) => (
              <li key={e.id} className={e.status.toLowerCase() === "failed" ? "failed" : undefined}>
                <div className="ws-agent-head">
                  <strong>{e.agent_name}</strong>
                  <Badge tone={stepTone(e.status)}>{stepLabel(e.status)}</Badge>
                </div>
                <div className="ws-agent-meta">
                  <code className="truncate">{e.task_key}</code>
                  <span>{e.duration_seconds != null ? `${Math.round(e.duration_seconds)} с` : duration(e.started_at, null)}</span>
                  {e.input_tokens + e.output_tokens > 0 && <span>{formatNumber(e.input_tokens + e.output_tokens)} ток.</span>}
                  {e.cost_usd > 0 && <span>{formatCost(e.cost_usd)}</span>}
                </div>
                {e.error && <p className="error ws-agent-error">{e.error}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

function TestsTab({ projectId }: { projectId: string }) {
  const runs = useProjectRuns(projectId);
  const audit = latest(runs.data, "audit");
  const detail = useRun(audit?.id);
  const [starting, setStarting] = useState(false);
  function start() {
    setStarting(true);
    api.createRun(projectId, "audit", {}).then(
      (run) => {
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: "success", title: "Аудит запущен", text: `Run ${run.id.slice(-8)}` });
        setStarting(false);
      },
      (err: unknown) => {
        toast({ tone: "danger", title: "Аудит не запущен", text: errorText(err) });
        setStarting(false);
      },
    );
  }
  const action = (
    <Button icon={<ShieldCheck size={14} />} loading={starting} onClick={start}>
      Запустить аудит
    </Button>
  );
  if (runs.isLoading) return <p className="muted ws-quiet">Загрузка…</p>;
  if (!audit)
    return (
      <div className="ws-tab-panel">
        <EmptyState icon={<FlaskConical size={20} />} title="Аудитов ещё не было" action={action}>
          Quality Gates (сборка, тесты, статический анализ, безопасность, покрытие) появятся после первого аудита оркестратора.
        </EmptyState>
      </div>
    );
  const gates = detail.data?.gates;
  return (
    <div className="ws-tab-panel">
      <div className="row ws-tab-bar">
        <span>
          Последний аудит: <a href={href("run", audit.id)}>run {audit.id.slice(-8)}</a> · {relativeTime(audit.created_at)}
        </span>
        <StateBadge state={audit.state} />
        <span className="card-spacer" />
        {action}
      </div>
      {detail.isLoading ? (
        <p className="muted">Загрузка…</p>
      ) : !gates || gates.length === 0 ? (
        <p className="muted">У этого запуска нет результатов Quality Gates.</p>
      ) : (
        <div className="scroll-x">
          <table className="ws-table">
            <thead>
              <tr>
                <th>Gate</th>
                <th>Результат</th>
                <th>Блокирует</th>
                <th>Причина</th>
              </tr>
            </thead>
            <tbody>
              {gates.map((g) => (
                <tr key={g.gate}>
                  <td>
                    <code>{g.gate}</code>
                  </td>
                  <td>
                    <GateBadge result={g.result} />
                  </td>
                  <td>{g.blocking ? "да" : "нет"}</td>
                  <td className="ws-reason">{g.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function LogsTab({ projectId }: { projectId: string }) {
  const runs = useProjectRuns(projectId);
  const last = (runs.data ?? [])[0];
  const detail = useRun(last?.id);
  if (runs.isLoading) return <p className="muted ws-quiet">Загрузка…</p>;
  if (!last)
    return (
      <div className="ws-tab-panel">
        <EmptyState icon={<ScrollText size={20} />} title="Журнала пока нет">
          Здесь появятся шаги, ошибки и задания последнего запуска оркестратора.
        </EmptyState>
      </div>
    );
  const run = detail.data;
  return (
    <div className="ws-tab-panel">
      <div className="row ws-tab-bar">
        <span>
          Последний запуск: <a href={href("run", last.id)}>{last.goal || last.kind}</a> · {relativeTime(last.created_at)}
        </span>
        <StateBadge state={last.state} />
      </div>
      {!run ? (
        <p className="muted">{detail.isLoading ? "Загрузка…" : "Подробности недоступны."}</p>
      ) : (
        <>
          {run.error && <pre className="ws-log error">{run.error}</pre>}
          <div className="scroll-x">
            <table className="ws-table">
              <thead>
                <tr>
                  <th>Шаг</th>
                  <th>Фаза</th>
                  <th>Состояние</th>
                  <th>Попытки</th>
                </tr>
              </thead>
              <tbody>
                {run.tasks.map((t) => (
                  <tr key={t.key}>
                    <td>
                      <code>{t.key}</code>
                      {t.error && <pre className="ws-log error">{t.error}</pre>}
                    </td>
                    <td>{t.phase}</td>
                    <td>
                      <Badge tone={stepTone(t.status)}>{stepLabel(t.status)}</Badge>
                    </td>
                    <td>{t.attempts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {run.jobs.length > 0 && (
            <>
              <h3 className="ws-subhead">Задания очереди</h3>
              <ul className="ws-jobs">
                {run.jobs.map((j, i) => (
                  <li key={`${j.kind}-${i}`}>
                    <code>{j.kind}</code> · {j.state} · попыток {j.attempts}
                    {j.error && <pre className="ws-log error">{j.error}</pre>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </div>
  );
}

function ArtifactsTab({ projectId }: { projectId: string }) {
  const runs = useProjectRuns(projectId);
  const last = latest(runs.data);
  const artifacts = useQuery({ queryKey: ["artifacts", last?.id], queryFn: () => api.artifacts(last?.id ?? ""), enabled: Boolean(last) });
  if (runs.isLoading) return <p className="muted ws-quiet">Загрузка…</p>;
  if (!last)
    return (
      <div className="ws-tab-panel">
        <EmptyState icon={<Package size={20} />} title="Артефактов нет">
          Отчёты (JSON, Markdown, SARIF) и доказательства появляются у запусков оркестратора.
        </EmptyState>
      </div>
    );
  return (
    <div className="ws-tab-panel">
      <div className="row ws-tab-bar">
        <span>
          Запуск <a href={href("run", last.id)}>{last.goal || last.kind}</a> · {relativeTime(last.created_at)}
        </span>
        <StateBadge state={last.state} />
      </div>
      {artifacts.isLoading ? (
        <p className="muted">Загрузка…</p>
      ) : artifacts.error ? (
        <p className="error">{errorText(artifacts.error)}</p>
      ) : (artifacts.data ?? []).length === 0 ? (
        <p className="muted">У этого запуска нет артефактов.</p>
      ) : (
        <ul className="ws-artifacts">
          {(artifacts.data ?? []).map((a) => (
            <li key={a.id}>
              <Package size={14} aria-hidden />
              <span className="truncate">{a.name}</span>
              <small>
                {a.media_type} · {formatNumber(a.size_bytes)} Б
              </small>
              <Button
                size="sm"
                variant="ghost"
                icon={<Download size={13} />}
                aria-label={`Скачать ${a.name}`}
                onClick={() =>
                  void download(`/runs/${last.id}/artifacts/${a.id}`, a.name).catch((err: unknown) =>
                    toast({ tone: "danger", title: "Не удалось скачать", text: errorText(err) }),
                  )
                }
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DirEntries({
  projectId,
  scope,
  path,
  onOpen,
  selected,
}: {
  projectId: string;
  scope: FileScope;
  path: string;
  onOpen: (path: string) => void;
  selected: string | null;
}) {
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.files(projectId, path, scope).then(setEntries, (err: unknown) => setError(errorText(err)));
  }, [projectId, path, scope]);

  if (error) return <li className="error">{error}</li>;
  if (!entries) return <li className="muted">…</li>;
  if (entries.length === 0 && path === "") return <li className="muted">Пусто</li>;
  return (
    <>
      {entries.map((entry) => {
        const full = path ? `${path}/${entry.name}` : entry.name;
        if (entry.type === "dir")
          return <TreeDir key={full} projectId={projectId} scope={scope} name={entry.name} path={full} onOpen={onOpen} selected={selected} />;
        if (entry.type !== "file")
          return (
            <li key={full} className="tree-item muted" title="Ссылки и специальные файлы не открываются">
              {entry.name}
            </li>
          );
        return (
          <li key={full}>
            <button type="button" className={selected === full ? "tree-item active" : "tree-item"} onClick={() => onOpen(full)}>
              {entry.name}
            </button>
          </li>
        );
      })}
    </>
  );
}

function TreeDir(props: {
  projectId: string;
  scope: FileScope;
  name: string;
  path: string;
  onOpen: (path: string) => void;
  selected: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <li>
      <button type="button" className="tree-item dir" aria-expanded={expanded} onClick={() => setExpanded((e) => !e)}>
        {expanded ? "▾" : "▸"} {props.name}
      </button>
      {expanded && (
        <ul>
          <DirEntries projectId={props.projectId} scope={props.scope} path={props.path} onOpen={props.onOpen} selected={props.selected} />
        </ul>
      )}
    </li>
  );
}

function EditorPane({
  projectId,
  file,
  editable,
  onEdit,
  onSaved,
  onReload,
  onDeleted,
}: {
  projectId: string;
  file: OpenFile;
  editable: boolean;
  onEdit: (text: string) => void;
  onSaved: (version: string, text: string) => void;
  onReload: () => void;
  onDeleted: () => void;
}) {
  const [state, setState] = useState<"idle" | "saving" | "saved" | "conflict">("idle");
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [savedText, setSavedText] = useState(file.content);
  const changed = file.content !== null && file.text !== savedText;

  async function save(force = false) {
    if (!editable || file.content === null) return;
    setState("saving");
    setError(null);
    try {
      const { version } = await api.saveFile(projectId, file.path, file.text, force ? null : file.version);
      setSavedText(file.text);
      onSaved(version, file.text);
      setState("saved");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) setState("conflict");
      else {
        setState("idle");
        setError(errorText(err));
      }
    }
  }

  async function remove() {
    try {
      await api.deleteFile(projectId, file.path);
      onDeleted();
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <div className="editor-pane">
      <div className="editor-bar">
        <code>{file.path}</code>
        {changed && <span title="Есть несохранённые изменения"> ●</span>}
        {state === "saved" && !changed && <span className="notice"> сохранено</span>}
        <span className="spacer" />
        {editable && file.content !== null && (
          <button type="button" className="primary" disabled={!changed || state === "saving"} onClick={() => void save()}>
            Сохранить
          </button>
        )}
        {editable &&
          (confirmDelete ? (
            <>
              <button type="button" className="danger" onClick={() => void remove()}>
                Удалить файл
              </button>
              <button type="button" onClick={() => setConfirmDelete(false)}>
                Отмена
              </button>
            </>
          ) : (
            <button type="button" onClick={() => setConfirmDelete(true)}>
              Удалить…
            </button>
          ))}
      </div>
      {state === "conflict" && (
        <p className="error" role="alert">
          Файл изменился после открытия (например, в терминале).{" "}
          <button type="button" onClick={onReload}>
            Загрузить заново
          </button>{" "}
          <button type="button" className="danger" onClick={() => void save(true)}>
            Перезаписать моей версией
          </button>
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {file.binary || file.tooLarge ? (
        <p className="muted ws-empty">
          {file.binary ? "Двоичный файл" : "Файл больше 1 МБ"} ({file.size} байт) — откройте его в терминале.
        </p>
      ) : (
        <Suspense fallback={<p className="muted">Загрузка редактора…</p>}>
          <CodeEditor path={file.path} content={file.content ?? ""} readOnly={!editable} onChange={onEdit} onSave={() => void save()} />
        </Suspense>
      )}
    </div>
  );
}

function NewFile({ projectId, onCreated }: { projectId: string; onCreated: (path: string) => void }) {
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const clean = path.trim().replace(/^\/+/, "");
    try {
      await api.saveFile(projectId, clean, "", null, true);
      setPath("");
      onCreated(clean);
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <form className="new-file" onSubmit={submit} aria-label="Новый файл">
      <input name="new-file" placeholder="новый файл: src/app.py" value={path} onChange={(e) => setPath(e.target.value)} required />
      <button type="submit">+</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

// Steps and agent executions report their status in either case ("SUCCEEDED", "succeeded").
const STEP_LABELS: Record<string, [string, "success" | "danger" | "accent" | "neutral" | "warning"]> = {
  SUCCEEDED: ["успешно", "success"],
  FAILED: ["ошибка", "danger"],
  RUNNING: ["выполняется", "accent"],
  PENDING: ["ожидает", "neutral"],
  READY: ["готов к запуску", "neutral"],
  SKIPPED: ["пропущен", "neutral"],
  CANCELLED: ["отменён", "neutral"],
  WAITING_APPROVAL: ["ждёт согласия", "warning"],
};
const stepLabel = (status: string) => STEP_LABELS[status.toUpperCase()]?.[0] ?? status;
const stepTone = (status: string) => STEP_LABELS[status.toUpperCase()]?.[1] ?? "neutral";

function GitPanel({
  projectId,
  prefix,
  onFilesChanged,
  onOpenFile,
  commitForm = true,
}: {
  projectId: string;
  prefix: string;
  onFilesChanged: () => void;
  /** Changed files open in the editor (the AI Workspace's Diff tab). */
  onOpenFile?: (path: string) => void;
  /** Under the review the commit is «Зафиксировать принятое»: no commit-everything here. */
  commitForm?: boolean;
}) {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [message, setMessage] = useState("");
  const [output, setOutput] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.gitStatus(projectId));
    } catch (err) {
      setOutput(errorText(err));
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function act(fn: () => Promise<string>) {
    setBusy(true);
    setOutput(null);
    try {
      setOutput(await fn());
      onFilesChanged();
      await refresh();
    } catch (err) {
      setOutput(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="git-panel">
      <div className="git-head">
        <strong>Git</strong> {status?.branch && <code>{status.branch}</code>}
        <span className="spacer" />
        <button type="button" disabled={busy} onClick={() => void refresh()}>
          Обновить
        </button>
      </div>
      {status && status.files.length === 0 && <p className="muted">Изменений нет.</p>}
      {status && status.files.length > 0 && (
        <ul className="git-files">
          {status.files.map((f) =>
            onOpenFile ? (
              <li key={f.path}>
                <code>{f.status}</code>{" "}
                <button type="button" className="link-btn" onClick={() => onOpenFile(f.path)}>
                  {f.path}
                </button>
              </li>
            ) : (
              <li key={f.path}>
                <code>{f.status}</code> {f.path}
              </li>
            ),
          )}
        </ul>
      )}
      {commitForm && (
      <form
        className="git-commit"
        onSubmit={(e) => {
          e.preventDefault();
          void act(async () => {
            const result = await api.commit(projectId, message);
            if (result.ok) setMessage("");
            return result.output || (result.ok ? "Зафиксировано." : "Нечего фиксировать.");
          });
        }}
      >
        <input name="commit-message" placeholder="Сообщение коммита" value={message} onChange={(e) => setMessage(e.target.value)} required />
        <button type="submit" disabled={busy}>
          Зафиксировать
        </button>
      </form>
      )}
      <div className="actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => void act(async () => (await api.pullProject(projectId)).output || "Уже актуально.")}
        >
          Обновить из проекта
        </button>
        <button
          type="button"
          disabled={busy}
          title={`Ветка ${prefix}<ваша ветка> в репозитории проекта`}
          onClick={() =>
            void act(async () => {
              const r = await api.publish(projectId);
              return `Отправлено в проект: ветка ${r.branch} (${r.commit.slice(0, 8)}).`;
            })
          }
        >
          Отправить в проект
        </button>
      </div>
      {output && <pre className="git-output">{output}</pre>}
    </div>
  );
}
