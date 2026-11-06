import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, chatUrl, errorText, getToken } from "./api";

// The AI chat of a workspace, like the Claude Code and Codex panels of VS Code: the
// official CLIs run in the background in the user's container (ai-coding-agent, see
// deploy/workspace/ai-coding-agent); this panel shows their events and answers their
// permission requests. Closing it does not stop the agent.

export type Agent = "claude" | "codex";
export type Mode = "ask" | "edits" | "plan" | "full";
export type Workflow = "create" | "develop" | "modernize" | "audit" | "maintain";

interface Meta {
  id: string;
  agent: Agent;
  mode: Mode;
  workflow?: Workflow | null; // none: a chat made before the modes
  planned?: boolean;
  title: string;
  updated?: number;
  model?: string;
}

interface AuthState {
  logged_in: boolean;
  method: string | null;
}

// Events of the chat daemon: {"t": kind, ...}; see Session.emit in ai-coding-agent.
type ChatEvent = { t: string; session?: string; [key: string]: unknown };

export const AGENTS: Record<Agent, string> = { claude: "Claude Code", codex: "Codex" };
export const MODES: Record<Mode, string> = {
  ask: "Спрашивать разрешения",
  edits: "Править файлы без вопросов",
  plan: "Только план",
  full: "Полный доступ",
};
// The platform's modes: a chat works in one, chosen when it starts. The agent gets the
// mode's process (WORKFLOWS in deploy/workspace/ai-coding-agent); the modes that change
// code begin with a plan the user approves, AUDIT only reads.
export const WORKFLOWS: Record<Workflow, { label: string; hint: string; placeholder: string; planFirst: boolean }> = {
  create: {
    label: "Создание",
    hint: "Новый проект по описанию: требования → архитектура → бэклог → каркас. Каждый этап — после вашего утверждения.",
    placeholder: "Опишите проект: что он делает, для кого, ограничения…",
    planFirst: true,
  },
  develop: {
    label: "Разработка",
    hint: "Функция или задача в проекте: агент изучит код и предложит план; после утверждения — ветка ai/…, код и тесты.",
    placeholder: "Какую функцию или задачу сделать?",
    planFirst: true,
  },
  modernize: {
    label: "Модернизация",
    hint: "Рефакторинг без изменения поведения: сначала тесты, затем план шагов; каждый шаг — отдельный коммит, его можно откатить.",
    placeholder: "Что модернизировать или отрефакторить?",
    planFirst: true,
  },
  audit: {
    label: "Аудит",
    hint: "Только чтение: безопасность, качество, архитектура, тесты. Отчёт с приоритетами; файлы не меняются.",
    placeholder: "Что проверить? Например: безопасность API",
    planFirst: false,
  },
  maintain: {
    label: "Сопровождение",
    hint: "Ошибки и зависимости: план исправлений, затем по одному — тест на ошибку, исправление, проверка.",
    placeholder: "Какую ошибку исправить или что обновить?",
    planFirst: true,
  },
};
const METHODS: Record<string, string> = {
  claudeai: "подписка Claude",
  console: "Anthropic Console (API)",
  api_key: "API-ключ",
  apiKey: "API-ключ",
  chatgpt: "ChatGPT",
};

const str = (value: unknown) => (typeof value === "string" ? value : value == null ? "" : String(value));

export function ChatPanel({
  projectId,
  onOpenTerminal,
  onFilesChanged,
  initialWorkflow,
  initialDraft,
}: {
  projectId: string;
  onOpenTerminal: (command: string) => void;
  onFilesChanged: () => void;
  /** A new AI task in this mode (e.g. from onboarding): start a new chat, not the last one. */
  initialWorkflow?: Workflow;
  /** A message to put in the box (e.g. "fix this finding"); the user sends it. */
  initialDraft?: string;
}) {
  const socket = useRef<WebSocket | null>(null);
  // Asked for a new task: do not reopen the latest chat when the daemon says hello.
  const freshStart = useRef(Boolean(initialWorkflow || initialDraft));
  const currentId = useRef<string | null>(null);
  const pendingText = useRef<{ text: string; mode: Mode; workflow: Workflow } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"connecting" | "open" | "closed">("connecting");
  const [attempt, setAttempt] = useState(0);
  const retries = useRef(0);
  const [sessions, setSessions] = useState<Meta[]>([]);
  const [auth, setAuth] = useState<Record<Agent, AuthState> | null>(null);
  const [current, setCurrent] = useState<Meta | null>(null);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [streams, setStreams] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [agent, setAgent] = useState<Agent>("claude");
  const [mode, setMode] = useState<Mode>("ask");
  const [workflow, setWorkflow] = useState<Workflow>(initialWorkflow ?? "develop");
  const [text, setText] = useState(initialDraft ?? "");
  const [login, setLogin] = useState<{ url: string; code: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stale, setStale] = useState(false); // the container's chat service predates the modes
  const changed = useRef(onFilesChanged);
  changed.current = onFilesChanged;

  const send = useCallback((message: object) => {
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify(message));
  }, []);

  useEffect(() => {
    const ws = new WebSocket(chatUrl(projectId));
    socket.current = ws;
    setState("connecting");

    function handle(ev: ChatEvent) {
      if (ev.type === "error") {
        setNotice(str(ev.message)); // the API refused the connection
        return;
      }
      switch (ev.t) {
        case "hello": {
          const metas = (ev.sessions as Meta[]) ?? [];
          setSessions(metas);
          if (!currentId.current && metas[0] && !freshStart.current) send({ op: "open", session: metas[0].id });
          return;
        }
        case "auth":
          setAuth(ev.agents as Record<Agent, AuthState>);
          return;
        case "opened": {
          const meta = ev.meta as Meta;
          currentId.current = meta.id;
          setCurrent(meta);
          setAgent(meta.agent);
          setMode(meta.mode);
          if (meta.workflow) setWorkflow(meta.workflow);
          setEvents((ev.history as ChatEvent[]) ?? []);
          setStreams({});
          setBusy(Boolean(ev.busy));
          setSessions((list) => [meta, ...list.filter((m) => m.id !== meta.id)]);
          const queued = pendingText.current;
          if (queued) {
            pendingText.current = null;
            if (!meta.workflow) {
              // An older image ignores the mode: never let the agent work without it.
              setStale(true);
              setText(queued.text);
              return;
            }
            send({ op: "send", session: meta.id, text: queued.text, mode: queued.mode });
          }
          return;
        }
        case "login":
          setLogin({ url: str(ev.url), code: str(ev.code) });
          return;
        case "login_done":
          setLogin(null);
          setNotice(ev.success ? "Вход выполнен." : `Вход не удался: ${str(ev.error)}`);
          return;
        case "closed":
          return;
      }
      if (!ev.session) {
        if (ev.t === "error") setNotice(str(ev.message));
        return;
      }
      if (ev.session !== currentId.current) return;
      if (ev.t === "delta") {
        const msg = str(ev.msg);
        setStreams((s) => ({ ...s, [msg]: (s[msg] ?? "") + str(ev.text) }));
        return;
      }
      if (ev.t === "thinking_delta") return;
      if (ev.t === "message") {
        const msg = str(ev.msg);
        setStreams((s) => {
          const { [msg]: _done, ...rest } = s;
          return rest;
        });
      }
      if (ev.t === "turn") {
        if (ev.state === "running") {
          setBusy(true);
          return;
        }
        setBusy(false);
        setStreams({});
        changed.current();
      }
      if (ev.t === "tool_result" && ev.diff) changed.current();
      if (ev.t === "planned") setCurrent((meta) => (meta ? { ...meta, planned: true } : meta));
      setEvents((list) => [...list, ev]);
    }

    ws.onopen = () => {
      ws.send(JSON.stringify({ token: getToken() }));
      ws.send(JSON.stringify({ op: "hello" }));
      // After a reconnection, follow the open chat again (its history is replayed).
      if (currentId.current) ws.send(JSON.stringify({ op: "open", session: currentId.current }));
      retries.current = 0;
      setState("open");
    };
    ws.onmessage = (message: MessageEvent<string>) => {
      try {
        handle(JSON.parse(message.data) as ChatEvent);
      } catch {
        /* not an event */
      }
    };
    let disposed = false;
    let timer = 0;
    ws.onclose = () => {
      if (disposed) return;
      // A restart of the server (an update) must not lose the chat: the agent keeps
      // working in the container, the panel comes back to it by itself.
      if (retries.current < 20) {
        retries.current += 1;
        setState("connecting");
        timer = window.setTimeout(() => setAttempt((n) => n + 1), Math.min(1000 * retries.current, 10000));
      } else {
        setState("closed");
      }
    };
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      ws.close();
    };
  }, [projectId, attempt, send]);

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end" });
  }, [events, streams]);

  function submit() {
    const message = text.trim();
    if (!message || busy || state !== "open") return;
    setText("");
    setNotice(null);
    if (!current || current.agent !== agent) {
      pendingText.current = { text: message, mode, workflow };
      send({ op: "new", agent, mode, workflow });
    } else {
      send({ op: "send", session: current.id, text: message, mode });
    }
  }

  function restart() {
    setNotice("Перезапуск среды…");
    api.restartWorkspace(projectId).then(
      () => {
        setNotice(null);
        setStale(false);
        currentId.current = null;
        setCurrent(null);
        setEvents([]);
        setAttempt((n) => n + 1);
      },
      (err: unknown) => setNotice(errorText(err)),
    );
  }

  // A plan the agent wrote as text (Codex has no plan mode): agreed from here.
  function approvePlan() {
    if (!current || busy || state !== "open") return;
    const remark = text.trim();
    setText("");
    setNotice(null);
    send({
      op: "send",
      session: current.id,
      text: remark ? `План утверждён. ${remark}` : "План утверждён — выполняй.",
      mode,
      approve_plan: true,
    });
  }

  function chooseWorkflow(next: Workflow) {
    setWorkflow(next);
    if (mode === "plan") setMode("ask"); // the modes plan by themselves
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function startNew(next: Agent) {
    currentId.current = null;
    setCurrent(null);
    setEvents([]);
    setStreams({});
    setBusy(false);
    setAgent(next);
    if (mode === "plan") setMode("ask");
  }

  const pending = useMemo(() => {
    const done = new Set(events.filter((e) => e.t === "approval_done").map((e) => str(e.id)));
    return new Set(events.filter((e) => e.t === "approval" && !done.has(str(e.id))).map((e) => str(e.id)));
  }, [events]);
  const results = useMemo(() => {
    const map = new Map<string, ChatEvent>();
    for (const e of events) if (e.t === "tool_result") map.set(str(e.id), e);
    return map;
  }, [events]);
  const tools = useMemo(() => new Set(events.filter((e) => e.t === "tool").map((e) => str(e.id))), [events]);
  // A permission request belongs to its tool call: shown inside the tool's card, as in VS Code.
  const approvals = useMemo(() => {
    const map = new Map<string, ChatEvent>();
    for (const e of events) if (e.t === "approval" && e.tool && tools.has(str(e.tool))) map.set(str(e.tool), e);
    return map;
  }, [events, tools]);
  const decisions = useMemo(() => {
    const map = new Map<string, string>();
    for (const e of events) if (e.t === "approval_done") map.set(str(e.id), str(e.decision));
    return map;
  }, [events]);

  // The chat's mode: an open chat keeps the one it was created in.
  const chatWorkflow: Workflow | null = current ? (current.workflow ?? null) : workflow;
  const rules = chatWorkflow ? WORKFLOWS[chatWorkflow] : null;
  const readOnly = chatWorkflow === "audit";
  const planned = Boolean(current?.planned) || events.some((e) => e.t === "planned");
  const awaitingPlan =
    Boolean(current && rules?.planFirst && !planned && !busy && pending.size === 0) &&
    events.some((e) => e.t === "message");
  const who = auth?.[agent];
  return (
    <div className="chat-panel">
      <div className="chat-head">
        <div className="segmented" role="tablist" aria-label="Агент">
          {(Object.keys(AGENTS) as Agent[]).map((a) => (
            <button
              key={a}
              type="button"
              role="tab"
              aria-selected={agent === a}
              className={agent === a ? "active" : undefined}
              onClick={() => (agent === a ? undefined : startNew(a))}
            >
              {AGENTS[a]}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <select
          aria-label="История чатов"
          value={current?.id ?? ""}
          onChange={(e) => (e.target.value ? send({ op: "open", session: e.target.value }) : startNew(agent))}
        >
          <option value="">Новый чат</option>
          {sessions.map((m) => (
            <option key={m.id} value={m.id}>
              {AGENTS[m.agent] ?? m.agent}
              {m.workflow && WORKFLOWS[m.workflow] ? ` · ${WORKFLOWS[m.workflow].label}` : ""}: {m.title}
            </option>
          ))}
        </select>
        <button type="button" title="Новый чат" aria-label="Новый чат" onClick={() => startNew(agent)}>
          +
        </button>
      </div>

      {state !== "open" && (
        <div className="chat-banner">
          {state === "connecting" ? "Подключение к агенту… (первый запуск контейнера — до минуты)" : "Соединение закрыто."}{" "}
          {state === "closed" && (
            <>
              <button type="button" onClick={() => setAttempt((n) => n + 1)}>
                Переподключиться
              </button>{" "}
              <button
                type="button"
                title="Новый контейнер из текущего образа. Агенты и терминалы остановятся; файлы и вход сохранятся."
                onClick={restart}
              >
                Перезапустить среду
              </button>
            </>
          )}
        </div>
      )}
      {stale && (
        <div className="chat-banner warn" role="alert">
          <b>Среда запущена из старого образа</b>: режимы работы в ней не действуют, поэтому задача не отправлена.
          Перезапустите среду — файлы, вход и история чатов сохранятся; открытые терминалы закроются.
          <div className="actions">
            <button type="button" className="primary" onClick={restart}>
              Перезапустить среду
            </button>
          </div>
        </div>
      )}
      {who && !who.logged_in && (
        <div className="chat-banner warn">
          {agent === "claude" ? (
            <>
              <b>Claude Code не подключён.</b> Войдите своим аккаунтом: подпиской Claude (Pro/Max) или Anthropic
              Console (оплата по API).
              <div className="actions">
                <button type="button" className="primary" onClick={() => onOpenTerminal("claude auth login")}>
                  Войти в терминале
                </button>
                <button type="button" onClick={() => onOpenTerminal("claude auth login --console")}>
                  Войти через Console
                </button>
                <button type="button" onClick={() => send({ op: "hello" })}>
                  Проверить вход
                </button>
              </div>
            </>
          ) : (
            <>
              <b>Codex не подключён.</b> Войдите своим аккаунтом ChatGPT (Plus/Pro) или API-ключом OpenAI.
              <div className="actions">
                <button type="button" className="primary" onClick={() => send({ op: "login", agent: "codex" })}>
                  Войти через ChatGPT
                </button>
                <button type="button" onClick={() => onOpenTerminal("codex login --with-api-key")}>
                  API-ключ в терминале
                </button>
              </div>
              {login && (
                <p>
                  Откройте{" "}
                  <a href={login.url} target="_blank" rel="noreferrer">
                    {login.url}
                  </a>{" "}
                  и введите код <code className="login-code">{login.code}</code>
                </p>
              )}
            </>
          )}
        </div>
      )}
      {who?.logged_in && (
        <p className="muted chat-who">
          {AGENTS[agent]} · {METHODS[who.method ?? ""] ?? who.method ?? "вход выполнен"}
          {current?.model ? ` · ${current.model}` : ""}
        </p>
      )}
      {current && rules && (
        <p className="chat-workflow-line" title={rules.hint}>
          Режим: <b>{rules.label}</b> ·{" "}
          {readOnly ? "только чтение" : planned ? "план утверждён" : "сначала план, изменения — после утверждения"}
        </p>
      )}
      {notice && (
        <p className="chat-banner" role="status">
          {notice}
        </p>
      )}

      <div className="chat-log" aria-live="polite">
        {!current && events.length === 0 && (
          <div className="chat-start">
            <p className="chat-start-title">Режим работы</p>
            <div className="chat-workflows" role="radiogroup" aria-label="Режим работы">
              {(Object.keys(WORKFLOWS) as Workflow[]).map((w) => (
                <button
                  key={w}
                  type="button"
                  role="radio"
                  aria-checked={workflow === w}
                  className={workflow === w ? "active" : undefined}
                  onClick={() => chooseWorkflow(w)}
                >
                  <b>{WORKFLOWS[w].label}</b>
                  <span>{WORKFLOWS[w].hint}</span>
                </button>
              ))}
            </div>
            <p className="muted">
              {AGENTS[agent]} работает в этом чате строго по процессу режима. Другой режим — новый чат (+). Агент
              продолжит работу, даже если закрыть страницу.
            </p>
          </div>
        )}
        {current && events.length === 0 && Object.keys(streams).length === 0 && (
          <p className="muted chat-empty">{rules?.hint ?? `Опишите задачу для ${AGENTS[agent]}.`}</p>
        )}
        {groupActivity(events).map((item, index) =>
          item.kind === "group" ? (
            <ActivityGroup
              key={`g${index}`}
              events={item.events}
              results={results}
              approvals={approvals}
              pending={pending}
              decisions={decisions}
              running={busy && index === lastGroup(events)}
              onDecide={(id, decision) => send({ op: "approve", session: current?.id, id, decision })}
            />
          ) : (
            <EventView
              key={`e${index}-${str(item.event.ts)}`}
              event={item.event}
              pending={pending.has(str(item.event.id))}
              decision={decisions.get(str(item.event.id))}
              onDecide={(decision) => send({ op: "approve", session: current?.id, id: item.event.id, decision })}
              onLogin={() => onOpenTerminal(agent === "claude" ? "claude auth login" : "codex login --device-auth")}
            />
          ),
        )}
        {Object.entries(streams).map(([msg, value]) => (
          <div key={msg} className="chat-msg assistant streaming">
            <Markdown text={value} />
          </div>
        ))}
        {busy && <p className="muted chat-working">{AGENTS[agent]} работает…</p>}
        <div ref={bottom} />
      </div>

      {awaitingPlan && (
        <div className="chat-plan-bar">
          <span>Изменения — только после утверждения плана. Есть замечания — напишите их агенту.</span>
          <button type="button" className="primary" onClick={approvePlan}>
            Утвердить план и выполнить
          </button>
        </div>
      )}
      <div className="chat-compose">
        <textarea
          aria-label="Сообщение"
          placeholder={`${rules?.placeholder ?? `Задача для ${AGENTS[agent]}…`} (Enter — отправить, Shift+Enter — новая строка)`}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          rows={3}
        />
        <div className="chat-compose-bar">
          {readOnly ? (
            <span className="muted" title="Режим «Аудит»: агент только читает, любые изменения отклоняются">
              Только чтение
            </span>
          ) : (
            <select aria-label="Разрешения" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
              {(Object.keys(MODES) as Mode[])
                .filter((m) => m !== "plan" || (agent === "claude" && !chatWorkflow))
                .map((m) => (
                  <option key={m} value={m}>
                    {MODES[m]}
                  </option>
                ))}
            </select>
          )}
          <span className="spacer" />
          {busy ? (
            <button type="button" className="danger" onClick={() => send({ op: "interrupt", session: current?.id })}>
              Остановить
            </button>
          ) : (
            <button type="button" className="primary" disabled={!text.trim() || state !== "open"} onClick={submit}>
              Отправить
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// Agent actions (commands, reads, edits, thinking) between two answers form one folded
// group, as in the VS Code extensions: the answers are the report, the rest on demand.
const ACTIVITY = new Set(["tool", "tool_result", "approval", "approval_done", "thinking", "notice"]);
const PLAN_TOOL = "ExitPlanMode"; // Claude Code's "the plan is ready": approved in the chat
type Item = { kind: "event"; event: ChatEvent } | { kind: "group"; events: ChatEvent[] };

export function groupActivity(events: ChatEvent[]): Item[] {
  const planTools = new Set(events.filter((e) => e.t === "tool" && e.name === PLAN_TOOL).map((e) => str(e.id)));
  const plans = new Set(events.filter((e) => e.t === "approval" && e.name === PLAN_TOOL).map((e) => str(e.id)));
  const items: Item[] = [];
  let group: ChatEvent[] | null = null;
  for (const event of events) {
    if ((event.t === "tool" || event.t === "tool_result") && planTools.has(str(event.id))) continue;
    if (event.t === "approval_done" && plans.has(str(event.id))) continue;
    if (ACTIVITY.has(event.t) && !(event.t === "approval" && plans.has(str(event.id)))) {
      if (group === null) {
        group = [];
        items.push({ kind: "group", events: group });
      }
      group.push(event);
    } else {
      group = null;
      items.push({ kind: "event", event });
    }
  }
  return items;
}

function lastGroup(events: ChatEvent[]): number {
  const items = groupActivity(events);
  for (let i = items.length - 1; i >= 0; i--) if (items[i]?.kind === "group") return i;
  return -1;
}

function oneLine(text: string, max = 90): string {
  const line = text.split("\n")[0] ?? "";
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function ActivityGroup({
  events,
  results,
  approvals,
  pending,
  decisions,
  running,
  onDecide,
}: {
  events: ChatEvent[];
  results: Map<string, ChatEvent>;
  approvals: Map<string, ChatEvent>;
  pending: Set<string>;
  decisions: Map<string, string>;
  running: boolean;
  onDecide: (id: unknown, decision: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const tools = events.filter((e) => e.t === "tool");
  const ids = new Set(tools.map((e) => str(e.id)));
  const loose = events.filter((e) => e.t === "approval" && !(e.tool && ids.has(str(e.tool))));
  const asking = events.some((e) => e.t === "approval" && pending.has(str(e.id)));
  const files = Array.from(
    new Set(tools.map((e) => str(results.get(str(e.id))?.file)).filter((f) => f.length > 0)),
  );
  const failed = tools.filter((e) => results.get(str(e.id))?.ok === false).length;
  const refused = events.filter((e) => e.t === "notice").length;
  const last = tools[tools.length - 1];
  const expanded = open || asking;
  const count = tools.length + loose.length;
  return (
    <div className={`chat-activity${asking ? " asking" : ""}`}>
      <button type="button" className="chat-activity-head" aria-expanded={expanded} onClick={() => setOpen((o) => !o)}>
        <span className="chat-caret">{expanded ? "▾" : "▸"}</span>
        {count > 0 ? `Действия: ${count}` : "Размышления"}
        {files.length > 0 && (
          <span className="muted">
            {" "}
            · изменено: {files.slice(0, 3).join(", ")}
            {files.length > 3 ? ` и ещё ${files.length - 3}` : ""}
          </span>
        )}
        {failed > 0 && <span className="error"> · с ошибкой: {failed}</span>}
        {refused > 0 && <span className="muted"> · отклонено (только чтение): {refused}</span>}
        {running && last && <span className="muted chat-now"> · {oneLine(str(last.title), 60)}</span>}
      </button>
      {expanded && (
        <div className="chat-activity-body">
          {events.map((e, i) => {
            if (e.t === "thinking")
              return (
                <details key={i} className="chat-thinking">
                  <summary>Размышления</summary>
                  <pre>{str(e.text)}</pre>
                </details>
              );
            if (e.t === "tool") {
              const approval = approvals.get(str(e.id));
              return (
                <ToolCard key={i} title={str(e.title)} detail={str(e.detail)} result={results.get(str(e.id))}>
                  {approval && (
                    <Permission
                      approval={approval}
                      pending={pending.has(str(approval.id))}
                      decision={decisions.get(str(approval.id))}
                      onDecide={(decision) => onDecide(approval.id, decision)}
                    />
                  )}
                </ToolCard>
              );
            }
            if (e.t === "approval" && loose.includes(e))
              return (
                <div key={i} className={pending.has(str(e.id)) ? "chat-approval pending" : "chat-approval"}>
                  <code>{oneLine(str(e.title))}</code>
                  {str(e.detail) && <pre className="chat-detail">{str(e.detail)}</pre>}
                  <Permission
                    approval={e}
                    pending={pending.has(str(e.id))}
                    decision={decisions.get(str(e.id))}
                    onDecide={(decision) => onDecide(e.id, decision)}
                  />
                </div>
              );
            if (e.t === "notice")
              return (
                <p key={i} className="muted chat-notice">
                  {str(e.text)}
                </p>
              );
            if (e.t === "tool_result" && !ids.has(str(e.id)))
              return <ToolCard key={i} title="Результат" detail="" result={e} />;
            return null;
          })}
        </div>
      )}
    </div>
  );
}

function EventView({
  event,
  pending,
  decision,
  onDecide,
  onLogin,
}: {
  event: ChatEvent;
  pending: boolean;
  decision: string | undefined;
  onDecide: (decision: string) => void;
  onLogin: () => void;
}) {
  switch (event.t) {
    case "approval": // only the plan's (groupActivity)
      return <PlanCard plan={str(event.detail)} pending={pending} decision={decision} onDecide={onDecide} />;
    case "planned":
      return <p className="muted chat-turn">✓ План утверждён — агент выполняет его</p>;
    case "user":
      return <div className="chat-msg user">{str(event.text)}</div>;
    case "message":
      return (
        <div className="chat-msg assistant">
          <Markdown text={str(event.text)} />
        </div>
      );
    case "turn":
      if (event.state === "done")
        return (
          <p className="muted chat-turn">
            ✓ Готово
            {typeof event.duration_ms === "number" ? ` за ${(event.duration_ms / 1000).toFixed(1)} с` : ""}
            {typeof event.cost === "number" && event.cost > 0 ? ` · $${event.cost.toFixed(4)}` : ""}
          </p>
        );
      if (event.state === "interrupted") return <p className="muted chat-turn">■ Остановлено {str(event.message)}</p>;
      return <p className="error chat-turn">Ошибка: {str(event.message) || "агент завершился с ошибкой"}</p>;
    case "auth_required":
      return (
        <div className="chat-banner warn">
          Нужен вход в {AGENTS[event.agent as Agent] ?? str(event.agent)}.{" "}
          <button type="button" onClick={onLogin}>
            Войти
          </button>
        </div>
      );
    case "error":
      return <p className="error chat-turn">{str(event.message)}</p>;
    default:
      return null;
  }
}

function Permission({
  approval,
  pending,
  decision,
  onDecide,
}: {
  approval: ChatEvent;
  pending: boolean;
  decision: string | undefined;
  onDecide: (decision: string) => void;
}) {
  if (!pending) return <p className="muted chat-decision">{DECISIONS[decision ?? ""] ?? decision ?? ""}</p>;
  return (
    <div className="chat-permission">
      <span>Нужно разрешение</span>
      <div className="actions">
        <button type="button" className="primary" onClick={() => onDecide("allow")}>
          Разрешить
        </button>
        {Boolean(approval.session_option) && (
          <button type="button" onClick={() => onDecide("allow_session")}>
            Разрешить в этой сессии
          </button>
        )}
        <button type="button" className="danger" onClick={() => onDecide("deny")}>
          Отклонить
        </button>
      </div>
    </div>
  );
}

function PlanCard({
  plan,
  pending,
  decision,
  onDecide,
}: {
  plan: string;
  pending: boolean;
  decision: string | undefined;
  onDecide: (decision: string) => void;
}) {
  return (
    <div className={`chat-plan${pending ? " pending" : ""}`}>
      <p className="chat-plan-title">План работы</p>
      {plan ? <Markdown text={plan} /> : <p className="muted">План — в сообщении агента выше.</p>}
      {pending ? (
        <>
          <div className="actions">
            <button type="button" className="primary" onClick={() => onDecide("allow")}>
              Утвердить план
            </button>
            <button type="button" onClick={() => onDecide("deny")}>
              Доработать
            </button>
          </div>
          <p className="muted chat-decision">«Доработать» — агент спросит, что изменить в плане.</p>
        </>
      ) : (
        <p className="muted chat-decision">{PLAN_DECISIONS[decision ?? ""] ?? ""}</p>
      )}
    </div>
  );
}

const PLAN_DECISIONS: Record<string, string> = {
  allow: "✓ План утверждён",
  allow_session: "✓ План утверждён",
  deny: "Отправлен на доработку",
  expired: "Не понадобилось",
};

const DECISIONS: Record<string, string> = {
  allow: "Разрешено",
  allow_session: "Разрешено до конца сессии",
  deny: "Отклонено",
  expired: "Не понадобилось",
};

function ToolCard({
  title,
  detail,
  result,
  children,
}: {
  title: string;
  detail: string;
  result: ChatEvent | undefined;
  children?: ReactNode;
}) {
  const diff = result ? str(result.diff) : "";
  const output = result ? str(result.text) : "";
  const asking = Boolean(children) && !result;
  return (
    <details
      className={`chat-tool${result && result.ok === false ? " failed" : ""}${asking ? " asking" : ""}`}
      open={asking}
    >
      <summary title={title}>
        <span className="chat-tool-mark">{!result ? "…" : result.ok === false ? "✗" : "✓"}</span>
        <code>{oneLine(title)}</code>
      </summary>
      {title.includes("\n") && <pre className="chat-detail">{title}</pre>}
      {detail && !diff && <pre className="chat-detail">{detail}</pre>}
      {diff && <Diff text={diff} />}
      {output && !diff && <pre className="chat-output">{output}</pre>}
      {children}
    </details>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="chat-diff">
      {text.split("\n").map((line, i) => (
        <span
          key={i}
          className={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : undefined}
        >
          {line}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

// A small, safe Markdown subset: fenced code blocks and `inline code`; the rest is text.
export function Markdown({ text }: { text: string }) {
  const parts = text.split(/```/);
  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          const body = part.replace(/^[\w+-]*\n/, "");
          return (
            <pre key={i} className="chat-code">
              <code>{body.replace(/\n$/, "")}</code>
            </pre>
          );
        }
        return (
          <p key={i} className="chat-text">
            {part.split("\n").map((line, j, lines) => {
              const heading = /^#{1,6}\s+(.*)$/.exec(line);
              return (
                <span key={j}>
                  {heading ? <b>{inline(heading[1] ?? "")}</b> : inline(line)}
                  {j < lines.length - 1 ? "\n" : ""}
                </span>
              );
            })}
          </p>
        );
      })}
    </>
  );
}

// Runs the agent started through the platform's tools (ADR-0038): open beside the code.
const RUN_LINK = /^#\/runs\/[0-9a-f-]{36}$/;

function inline(text: string): ReactNode[] {
  return text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*|#\/runs\/[0-9a-f-]{36})/).map((chunk, i) => {
    if (RUN_LINK.test(chunk))
      return (
        <a key={i} href={chunk} target="_blank" rel="noreferrer">
          {chunk}
        </a>
      );
    if (chunk.startsWith("`") && chunk.endsWith("`") && chunk.length > 2) {
      const body = chunk.slice(1, -1);
      return RUN_LINK.test(body) ? (
        <a key={i} href={body} target="_blank" rel="noreferrer">
          <code>{body}</code>
        </a>
      ) : (
        <code key={i}>{body}</code>
      );
    }
    if (chunk.startsWith("**") && chunk.endsWith("**") && chunk.length > 4) return <b key={i}>{chunk.slice(2, -2)}</b>;
    return chunk;
  });
}
