import { Gauge, History as HistoryIcon, Plus, RefreshCw, Trash2, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { api, chatUrl, errorText, getToken } from "./api";
import { useChatBridge } from "./review/inline";
import { Progress, type Tone } from "./ui";

// The AI chat of a workspace, like the Claude Code and Codex panels of VS Code: the
// official CLIs run in the background in the user's container (ai-coding-agent, see
// deploy/workspace/ai-coding-agent); this panel shows their events and answers their
// permission requests. Several chats are open at once, in tabs (ADR-0042); closing a tab,
// or the page, does not stop its agent.

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
  plan?: string | null; // Claude: the claude.ai subscription (pro, max…)
}

// A limit of the user's plan, as the chat service reads it from the official CLIs (op
// "usage"): Claude Code's /usage, Codex's rate limits. Times are Unix seconds.
export interface Limit {
  window: string; // "session" (the 5-hour window), "week", or the vendor's own kind
  minutes: number | null;
  scope: string | null; // the model a window is for
  percent: number;
  resets_at: number | null;
  severity?: string | null;
}

export interface Usage {
  agent: Agent;
  at: number;
  limits: Limit[];
  plan?: string | null;
  text?: string; // the CLI's own words (an account without plan limits)
  error?: string;
  reached?: string | null;
}

// Events of the chat daemon: {"t": kind, ...}; see Session.emit in ai-coding-agent.
type ChatEvent = { t: string; session?: string; [key: string]: unknown };

// An open chat: a tab. A new chat is a draft ("new:1") until its first message creates it.
interface Chat {
  meta: Meta | null;
  events: ChatEvent[];
  streams: Record<string, string>;
  busy: boolean;
  loaded: boolean; // its history came
}

interface Tabs {
  tabs: string[];
  active: string | null;
  chats: Record<string, Chat>;
}

interface Composer {
  agent: Agent;
  mode: Mode;
  workflow: Workflow;
  text: string;
}

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
  "claude.ai": "подписка Claude",
  console: "Anthropic Console (API)",
  api_key: "API-ключ",
  apiKey: "API-ключ",
  chatgpt: "ChatGPT",
};

const str = (value: unknown) => (typeof value === "string" ? value : value == null ? "" : String(value));

const NEW_TITLE = "Новый чат";
const DRAFT = "new:";
const MAX_TABS = 8; // beyond, the oldest idle tab closes (its agent goes on)
const NO_EVENTS: ChatEvent[] = [];
const NO_STREAMS: Record<string, string> = {};
const isDraft = (key: string) => key.startsWith(DRAFT);
const draftChat = (): Chat => ({ meta: null, events: NO_EVENTS, streams: NO_STREAMS, busy: false, loaded: true });
// A chat of the history whose events have not come yet.
const pendingChat = (meta: Meta | null): Chat => ({ meta, events: NO_EVENTS, streams: NO_STREAMS, busy: false, loaded: false });

// The open tabs of a project, remembered in this browser: the panel comes back to them.
const tabsKey = (projectId: string) => `ai-coding-chat-tabs:${projectId}`;

function loadTabs(projectId: string): { tabs: string[]; active: string | null } {
  try {
    const data: unknown = JSON.parse(localStorage.getItem(tabsKey(projectId)) ?? "null");
    if (data && typeof data === "object" && Array.isArray((data as { tabs?: unknown }).tabs)) {
      const { tabs, active } = data as { tabs: unknown[]; active?: unknown };
      return {
        tabs: tabs.filter((t): t is string => typeof t === "string" && !isDraft(t)),
        active: typeof active === "string" ? active : null,
      };
    }
  } catch {
    /* no storage, or not ours */
  }
  return { tabs: [], active: null };
}

function saveTabs(projectId: string, tabs: string[], active: string | null) {
  try {
    localStorage.setItem(
      tabsKey(projectId),
      JSON.stringify({ tabs: tabs.filter((t) => !isDraft(t)), active: active && !isDraft(active) ? active : null }),
    );
  } catch {
    /* no storage: the tabs are not remembered */
  }
}

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
  // Asked for a new task: open a new chat, not the last ones, when the daemon says hello.
  const freshStart = useRef(Boolean(initialWorkflow || initialDraft));
  const restored = useRef(false); // the tabs came back (the first hello)
  const resubscribe = useRef(false); // reconnected: follow the open tabs again
  // What the chat service offers beyond its first protocol (an older image has none).
  const features = useRef<Set<string>>(new Set());
  const [featureList, setFeatureList] = useState<string[]>([]);
  // The tabs live in a ref, read and changed by the socket's handler; a counter renders.
  const store = useRef<Tabs>({ tabs: [], active: null, chats: {} });
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  const drafts = useRef(0);
  const creating = useRef(new Map<string, { text: string; mode: Mode; workflow: Workflow }>()); // draft -> its first message
  const closed = useRef(new Set<string>()); // tabs closed while their chat was opening
  const stash = useRef<Record<string, Composer>>({}); // each tab's composer, while another is shown
  const bottom = useRef<HTMLDivElement>(null);
  const textBox = useRef<HTMLTextAreaElement>(null);
  const [state, setState] = useState<"connecting" | "open" | "closed">("connecting");
  const [attempt, setAttempt] = useState(0);
  const retries = useRef(0);
  const [sessions, setSessions] = useState<Meta[]>([]);
  const [auth, setAuth] = useState<Record<Agent, AuthState> | null>(null);
  const [usage, setUsage] = useState<Partial<Record<Agent, Usage>>>({});
  const [agent, setAgent] = useState<Agent>("claude");
  const [mode, setMode] = useState<Mode>("ask");
  const [workflow, setWorkflow] = useState<Workflow>(initialWorkflow ?? "develop");
  const [text, setText] = useState(initialDraft ?? "");
  const [login, setLogin] = useState<{ url: string; code: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stale, setStale] = useState(false); // the container's chat service predates the modes
  const [menu, setMenu] = useState<"history" | "limits" | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null); // the chat to delete, once confirmed
  const ui = useRef<Composer>({ agent, mode, workflow, text });
  ui.current = { agent, mode, workflow, text };
  const changed = useRef(onFilesChanged);
  changed.current = onFilesChanged;
  // Seconds the browser's clock is ahead of the daemon's (events carry its "ts"): elapsed
  // times are measured on the daemon's clock, whatever the user's computer says.
  const skew = useRef(0);

  const { tabs, active, chats } = store.current;
  const chat = active ? chats[active] : undefined;
  const current = chat?.meta ?? null;
  const events = chat?.events ?? NO_EVENTS;
  const streams = chat?.streams ?? NO_STREAMS;
  const busy = chat?.busy ?? false;
  const loading = Boolean(active && !isDraft(active) && chat && !chat.loaded);

  const send = useCallback((message: object) => {
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify(message));
  }, []);

  // ------------------------------------------------------------------ tabs --
  function commit(next: Partial<Tabs>) {
    store.current = { ...store.current, ...next };
    if (next.tabs || next.active !== undefined) saveTabs(projectId, store.current.tabs, store.current.active);
    rerender();
  }

  function setChat(id: string, value: Chat) {
    commit({ chats: { ...store.current.chats, [id]: value } });
  }

  function addTab(key: string, value: Chat) {
    const { active } = store.current;
    let { tabs, chats } = store.current;
    if (!tabs.includes(key)) tabs = [...tabs, key];
    chats = { ...chats, [key]: chats[key] ?? value };
    if (tabs.length > MAX_TABS) {
      const old = tabs.find((k) => k !== key && k !== active && !chats[k]?.busy && !creating.current.has(k));
      if (old) {
        tabs = tabs.filter((k) => k !== old);
        const { [old]: _gone, ...rest } = chats;
        chats = rest;
        delete stash.current[old];
        if (!isDraft(old) && features.current.has("tabs")) send({ op: "close", session: old });
      }
    }
    commit({ tabs, chats });
  }

  // Show a tab: its composer comes back. An older chat service follows one chat at a
  // time: the chat is opened again (its history is replayed).
  function activate(key: string) {
    const { active: shown, tabs, chats } = store.current;
    if (shown === key) return;
    if (shown && tabs.includes(shown)) stash.current[shown] = { ...ui.current };
    commit({ active: key });
    const saved = stash.current[key];
    const meta = chats[key]?.meta;
    if (meta) {
      setAgent(meta.agent);
      setMode(saved?.mode ?? meta.mode);
      if (meta.workflow) setWorkflow(meta.workflow);
    } else if (saved) {
      setAgent(saved.agent);
      setMode(saved.mode);
      setWorkflow(saved.workflow);
    }
    setText(saved?.text ?? "");
    setNotice(null);
    if (!isDraft(key) && (!chats[key]?.loaded || !features.current.has("tabs"))) send({ op: "open", session: key });
  }

  // Close a tab: its agent goes on (the chat stays in the history). "gone": the chat was
  // deleted.
  function closeTab(key: string, { gone = false }: { gone?: boolean } = {}) {
    const { tabs, active, chats } = store.current;
    const index = tabs.indexOf(key);
    if (index < 0) return;
    const rest = tabs.filter((k) => k !== key);
    const { [key]: _closed, ...others } = chats;
    delete stash.current[key];
    creating.current.delete(key);
    if (!isDraft(key) && !gone) {
      if (!chats[key]?.loaded) closed.current.add(key);
      if (features.current.has("tabs")) send({ op: "close", session: key });
    }
    commit({ tabs: rest, chats: others });
    if (active !== key) return;
    const next = rest[index] ?? rest[index - 1];
    if (next) activate(next);
    else startNew(ui.current.agent);
  }

  // A new chat (a draft tab): an empty one is reused.
  function startNew(next: Agent) {
    const empty = store.current.tabs.find((k) => isDraft(k) && !creating.current.has(k));
    const key = empty ?? `${DRAFT}${++drafts.current}`;
    if (!empty) addTab(key, draftChat());
    activate(key);
    setAgent(next);
    setMode((m) => (m === "plan" ? "ask" : m));
  }

  function openChat(id: string) {
    setMenu(null);
    closed.current.delete(id);
    if (!store.current.tabs.includes(id)) addTab(id, pendingChat(sessions.find((m) => m.id === id) ?? null));
    activate(id);
  }

  // Follow the open tabs (their histories come again), the shown one first. An older chat
  // service follows one chat at a time: the others load when shown.
  function follow() {
    const { tabs, active } = store.current;
    const first = active && !isDraft(active) ? [active] : [];
    const rest = features.current.has("tabs") ? tabs.filter((k) => !isDraft(k) && k !== active) : [];
    for (const id of [...first, ...rest]) send({ op: "open", session: id });
  }

  // The first hello: the tabs this browser had open (those still there), else the latest
  // chat, or a new one when asked for a task.
  function restore(metas: Meta[]) {
    const known = new Map(metas.map((m) => [m.id, m]));
    const saved = loadTabs(projectId);
    let tabs = saved.tabs.filter((id) => known.has(id)).slice(-MAX_TABS);
    if (!freshStart.current && tabs.length === 0 && metas[0]) tabs = [metas[0].id];
    let active = saved.active && tabs.includes(saved.active) ? saved.active : (tabs[0] ?? null);
    const chats: Record<string, Chat> = {};
    for (const id of tabs) chats[id] = pendingChat(known.get(id) ?? null);
    if (freshStart.current || !active) {
      active = `${DRAFT}${++drafts.current}`;
      tabs = [...tabs, active];
      chats[active] = draftChat();
    }
    commit({ tabs, active, chats });
    follow();
  }

  function readLimits(agents: Record<Agent, AuthState> | null, refresh: boolean) {
    if (!agents || !features.current.has("usage")) return;
    for (const a of Object.keys(AGENTS) as Agent[])
      if (agents[a]?.logged_in) send({ op: "usage", agent: a, ...(refresh ? { refresh: true } : {}) });
  }

  // ---------------------------------------------------------------- events --
  function opened(ev: ChatEvent) {
    const meta = ev.meta as Meta;
    if (typeof ev.now === "number") skew.current = Date.now() / 1000 - ev.now;
    setSessions((list) => [meta, ...list.filter((m) => m.id !== meta.id)]);
    let { tabs, active, chats } = store.current;
    let queued: { text: string; mode: Mode; workflow: Workflow } | undefined;
    if (!tabs.includes(meta.id)) {
      // A new chat: its draft (older chat services do not say which: the first waiting).
      const ref = typeof ev.ref === "string" ? ev.ref : creating.current.keys().next().value;
      queued = ref === undefined ? undefined : creating.current.get(ref);
      if (ref !== undefined && queued !== undefined) {
        creating.current.delete(ref);
        tabs = tabs.map((k) => (k === ref ? meta.id : k));
        if (active === ref) active = meta.id;
        const { [ref]: _draft, ...others } = chats;
        chats = others;
        const saved = stash.current[ref];
        delete stash.current[ref];
        if (saved) stash.current[meta.id] = saved;
      } else if (typeof ev.ref === "string" || closed.current.delete(meta.id)) {
        // Its tab was closed meanwhile: the chat stays in the history.
        if (features.current.has("tabs")) send({ op: "close", session: meta.id });
        return;
      } else {
        // Opened by the chat service itself: shown, in place of an untouched new chat.
        const blank = active && isDraft(active) && !creating.current.has(active) && !ui.current.text.trim();
        tabs = blank ? tabs.map((k) => (k === active ? meta.id : k)) : [...tabs, meta.id];
        if (blank && active) {
          const { [active]: _draft, ...others } = chats;
          chats = others;
        }
        active = meta.id;
      }
    }
    const history = (ev.history as ChatEvent[] | undefined) ?? NO_EVENTS;
    chats = { ...chats, [meta.id]: { meta, events: history, streams: NO_STREAMS, busy: Boolean(ev.busy), loaded: true } };
    commit({ tabs, active, chats });
    if (active === meta.id) {
      setAgent(meta.agent);
      setMode(meta.mode);
      if (meta.workflow) setWorkflow(meta.workflow);
    }
    if (!queued) return;
    if (!meta.workflow) {
      // An older image ignores the mode: never let the agent work without it.
      setStale(true);
      if (active === meta.id) setText(queued.text);
      else stash.current[meta.id] = { ...(stash.current[meta.id] ?? ui.current), text: queued.text };
      return;
    }
    send({ op: "send", session: meta.id, text: queued.text, mode: queued.mode });
  }

  function chatEvent(ev: ChatEvent) {
    const id = str(ev.session);
    let value = store.current.chats[id];
    if (!value?.loaded) return; // not followed here: closed, or its history is on its way
    if (typeof ev.ts === "number") skew.current = Date.now() / 1000 - ev.ts; // a live event
    if (ev.t === "delta") {
      const msg = str(ev.msg);
      setChat(id, { ...value, streams: { ...value.streams, [msg]: (value.streams[msg] ?? "") + str(ev.text) } });
      return;
    }
    if (ev.t === "thinking_delta") return;
    if (ev.t === "message") {
      const { [str(ev.msg)]: _done, ...rest } = value.streams;
      value = { ...value, streams: rest };
    }
    if (ev.t === "turn") {
      if (ev.state === "running") {
        setChat(id, { ...value, busy: true });
        return;
      }
      value = { ...value, busy: false, streams: NO_STREAMS };
      changed.current();
    }
    if (ev.t === "tool_result" && ev.diff) changed.current();
    if (value.meta && ev.t === "planned") value = { ...value, meta: { ...value.meta, planned: true } };
    if (value.meta && ev.t === "user" && value.meta.title === NEW_TITLE) {
      // Titled by its first message, as the chat service does.
      const meta = { ...value.meta, title: str(ev.text).split("\n")[0]?.slice(0, 80) || NEW_TITLE };
      value = { ...value, meta };
      setSessions((list) => list.map((m) => (m.id === id ? meta : m)));
    }
    setChat(id, { ...value, events: [...value.events, ev] });
  }

  useEffect(() => {
    // Another project, or the panel goes: the tabs are the project's own.
    return () => {
      store.current = { tabs: [], active: null, chats: {} };
      restored.current = false;
      creating.current.clear();
      closed.current.clear();
      stash.current = {};
    };
  }, [projectId]);

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
          const list = Array.isArray(ev.features) ? ev.features.map(str) : [];
          features.current = new Set(list);
          setFeatureList(list);
          if (!restored.current) {
            restored.current = true;
            restore(metas);
            return;
          }
          // Chats deleted meanwhile lose their tabs; after a reconnection the others are
          // followed again.
          const known = new Set(metas.map((m) => m.id));
          for (const key of store.current.tabs) if (!isDraft(key) && !known.has(key)) closeTab(key, { gone: true });
          if (resubscribe.current) {
            resubscribe.current = false;
            follow();
          }
          return;
        }
        case "auth": {
          const agents = ev.agents as Record<Agent, AuthState>;
          setAuth(agents);
          readLimits(agents, false);
          return;
        }
        case "usage":
          setUsage((all) => ({ ...all, [str(ev.agent)]: ev as unknown as Usage }));
          return;
        case "opened":
          opened(ev);
          return;
        case "deleted": {
          const id = str(ev.session);
          setSessions((list) => list.filter((m) => m.id !== id));
          setConfirm((c) => (c === id ? null : c));
          closeTab(id, { gone: true });
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
      chatEvent(ev);
    }

    ws.onopen = () => {
      ws.send(JSON.stringify({ token: getToken() }));
      ws.send(JSON.stringify({ op: "hello" }));
      // After a reconnection, the answer to hello brings the open tabs back.
      resubscribe.current = restored.current;
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the handler works on refs
  }, [projectId, attempt, send]);

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end" });
  }, [events, streams]);

  // A window of the plan resets: read the limits again then.
  useEffect(() => {
    const now = Date.now() / 1000 - skew.current;
    const resets = Object.values(usage).flatMap((u) => (u?.limits ?? []).map((l) => l.resets_at ?? 0));
    const next = Math.min(...resets.filter((t) => t > now));
    if (!Number.isFinite(next)) return;
    const timer = window.setTimeout(() => readLimits(auth, false), Math.min((next - now + 5) * 1000, 6 * 3600 * 1000));
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- readLimits sends on the socket
  }, [usage, auth]);

  function submit() {
    if (submitText(text.trim())) setText("");
  }

  function submitText(message: string): boolean {
    const key = store.current.active;
    if (!message || !key || busy || state !== "open" || creating.current.has(key)) return false;
    setNotice(null);
    if (!current) {
      creating.current.set(key, { text: message, mode, workflow });
      send({ op: "new", agent, mode, workflow, ref: key });
    } else {
      send({ op: "send", session: current.id, text: message, mode });
    }
    return true;
  }

  // AI actions of the editor (explain, fix, improve, tests) arrive here: sent at once, or
  // put in the box for the user (a question to finish, or while the agent is still busy).
  const request = useChatBridge((s) => s.request);
  useEffect(() => {
    if (!request || request.projectId !== projectId) return;
    if (!request.send || busy || stale) {
      setText(request.text);
      if (request.send) setNotice("Агент ещё работает: запрос в поле ввода — отправьте его, когда он закончит.");
      textBox.current?.focus();
      useChatBridge.getState().done(request.id);
      return;
    }
    if (state !== "open") return; // sent once connected
    if (submitText(request.text)) useChatBridge.getState().done(request.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- submitText reads the current state
  }, [request, state, busy, stale, projectId]);

  function restart() {
    setNotice("Перезапуск среды…");
    api.restartWorkspace(projectId).then(
      () => {
        setNotice(null);
        // The chat the old image made has no mode: the task goes to a new chat.
        freshStart.current = stale;
        setStale(false);
        store.current = { tabs: [], active: null, chats: {} };
        restored.current = false;
        creating.current.clear();
        rerender();
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

  function toggle(which: "history" | "limits") {
    setConfirm(null);
    setMenu((m) => (m === which ? null : which));
    if (which === "limits" && menu !== "limits") readLimits(auth, false);
  }
  const toolbar = useDismiss(menu !== null, () => {
    setMenu(null);
    setConfirm(null);
  });

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
  // While the agent works, what it waits for: the user's answer, or an action still running.
  // A long command (installing dependencies, a test run) shows its output only at the end:
  // its time keeps counting, so that it does not look stuck.
  const running = useMemo(() => (busy && pending.size === 0 ? runningAction(events) : null), [busy, pending, events]);
  const runningId = running?.id ?? null;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!runningId && menu !== "limits") return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), runningId ? 1000 : 30_000);
    return () => window.clearInterval(timer);
  }, [runningId, menu]);
  const elapsed = running ? Math.max(0, Math.floor(now / 1000 - skew.current - running.since)) : 0;
  // A tab that waits for the user's answer is marked (its events change only with them).
  const asking = useRef(new WeakMap<ChatEvent[], boolean>());
  function waits(list: ChatEvent[]): boolean {
    let value = asking.current.get(list);
    if (value === undefined) {
      const done = new Set(list.filter((e) => e.t === "approval_done").map((e) => str(e.id)));
      value = list.some((e) => e.t === "approval" && !done.has(str(e.id)));
      asking.current.set(list, value);
    }
    return value;
  }

  // The chat's mode: an open chat keeps the one it was created in.
  const chatWorkflow: Workflow | null = current ? (current.workflow ?? null) : workflow;
  const rules = chatWorkflow ? WORKFLOWS[chatWorkflow] : null;
  const readOnly = chatWorkflow === "audit";
  const planned = Boolean(current?.planned) || events.some((e) => e.t === "planned");
  const awaitingPlan =
    Boolean(current && rules?.planFirst && !planned && !busy && pending.size === 0) &&
    events.some((e) => e.t === "message");
  const who = auth?.[agent];
  const showLimits = featureList.includes("usage") && Boolean(who?.logged_in);
  const canDelete = featureList.includes("delete");
  const daemonNow = now / 1000 - skew.current;
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
        <div className="chat-tools" ref={toolbar}>
          {showLimits && (
            <LimitsChip usage={usage[agent]} expanded={menu === "limits"} onClick={() => toggle("limits")} />
          )}
          <button
            type="button"
            className="chat-tool-button"
            aria-expanded={menu === "history"}
            title="Все чаты: открыть во вкладке, удалить"
            onClick={() => toggle("history")}
          >
            <HistoryIcon size={14} aria-hidden />
            Все чаты{sessions.length > 0 ? ` (${sessions.length})` : ""}
          </button>
          {menu === "limits" && (
            <div className="chat-pop chat-limits" role="dialog" aria-label="Лимиты подписки">
              <div className="chat-pop-head">
                <b>Лимиты подписки</b>
                <button type="button" title="Прочитать заново" onClick={() => readLimits(auth, true)}>
                  <RefreshCw size={13} aria-hidden /> Обновить
                </button>
              </div>
              {(Object.keys(AGENTS) as Agent[])
                .filter((a) => auth?.[a]?.logged_in)
                .map((a) => (
                  <AgentLimits key={a} agent={a} auth={auth?.[a]} usage={usage[a]} now={daemonNow} />
                ))}
            </div>
          )}
          {menu === "history" && (
            <div className="chat-pop chat-history" role="dialog" aria-label="Все чаты">
              {sessions.length === 0 ? (
                <p className="muted">Чатов пока нет.</p>
              ) : (
                <ul>
                  {sessions.map((m) => {
                    const working = Boolean(chats[m.id]?.busy);
                    return (
                      <li key={m.id} className={tabs.includes(m.id) ? "open" : undefined}>
                        {confirm === m.id ? (
                          <div className="chat-history-confirm" role="group" aria-label="Подтверждение удаления">
                            <span>
                              Удалить «{m.title}»? Переписка удалится, файлы рабочей области останутся.
                            </span>
                            <div className="actions">
                              <button
                                type="button"
                                className="danger"
                                onClick={() => {
                                  setConfirm(null);
                                  send({ op: "delete", session: m.id });
                                }}
                              >
                                Удалить
                              </button>
                              <button type="button" onClick={() => setConfirm(null)}>
                                Отмена
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <button type="button" className="chat-history-open" onClick={() => openChat(m.id)}>
                              <span className="chat-history-title">{m.title}</span>
                              <small className="muted">
                                {AGENTS[m.agent] ?? m.agent}
                                {m.workflow && WORKFLOWS[m.workflow] ? ` · ${WORKFLOWS[m.workflow].label}` : ""}
                                {m.updated ? ` · ${ago(daemonNow - m.updated)}` : ""}
                                {working ? " · работает" : ""}
                              </small>
                            </button>
                            {canDelete && (
                              <button
                                type="button"
                                className="chat-history-delete"
                                aria-label={`Удалить чат «${m.title}»`}
                                title={working ? "Агент работает — сначала остановите его" : "Удалить чат"}
                                disabled={working}
                                onClick={() => setConfirm(m.id)}
                              >
                                <Trash2 size={14} aria-hidden />
                              </button>
                            )}
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
              {!canDelete && state === "open" && (
                <p className="muted chat-pop-note">
                  Удаление чатов и вкладки в фоне — после «Перезапустить среду»: среда запущена из старого образа.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="chat-tabs" role="tablist" aria-label="Открытые чаты">
        {tabs.map((key) => {
          const tab = chats[key];
          const title = tab?.meta?.title ?? NEW_TITLE;
          const waiting = tab ? waits(tab.events) : false;
          const meta = tab?.meta;
          return (
            <div key={key} className={`chat-tab${key === active ? " active" : ""}`}>
              <button
                type="button"
                role="tab"
                aria-selected={key === active}
                title={
                  meta
                    ? `${AGENTS[meta.agent] ?? meta.agent}${meta.workflow && WORKFLOWS[meta.workflow] ? ` · ${WORKFLOWS[meta.workflow].label}` : ""}: ${title}`
                    : NEW_TITLE
                }
                onClick={() => activate(key)}
              >
                {(tab?.busy || waiting) && (
                  <span className={`chat-tab-dot${waiting ? " waiting" : ""}`} aria-hidden />
                )}
                <span className="chat-tab-title">{title}</span>
                {waiting ? (
                  <span className="sr-only"> — ждёт вашего ответа</span>
                ) : tab?.busy ? (
                  <span className="sr-only"> — работает</span>
                ) : null}
              </button>
              <button
                type="button"
                className="chat-tab-close"
                aria-label={`Закрыть вкладку «${title}»`}
                title="Закрыть вкладку (агент продолжит работу, чат останется во «Всех чатах»)"
                onClick={() => closeTab(key)}
              >
                <X size={12} aria-hidden />
              </button>
            </div>
          );
        })}
        <button type="button" className="chat-tab-new" title="Новый чат" aria-label="Новый чат" onClick={() => startNew(agent)}>
          <Plus size={14} aria-hidden />
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
          {who.plan ? ` ${planName(who.plan)}` : ""}
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
        {loading && <p className="muted chat-empty">Загрузка чата…</p>}
        {current && !loading && events.length === 0 && Object.keys(streams).length === 0 && (
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
        {busy && (
          <p className="muted chat-working">
            {pending.size > 0 ? (
              `${AGENTS[agent]} ждёт вашего решения — ответьте на запрос выше.`
            ) : running ? (
              <>
                {AGENTS[agent]} выполняет <code>{oneLine(running.title, 60)}</code> ·{" "}
                {/* not read aloud every second */}
                <span aria-live="off">{duration(elapsed)}</span>
              </>
            ) : (
              `${AGENTS[agent]} работает…`
            )}
          </p>
        )}
        {running && elapsed >= LONG_ACTION_SECONDS && (
          <p className="muted chat-long">
            Команда ещё выполняется — это не зависание: установка зависимостей, сборка или тесты могут идти несколько
            минут, а её вывод появится по завершении. Прервать — кнопка «Остановить».
          </p>
        )}
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
          ref={textBox}
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

// ---------------------------------------------------------------- limits --
const hours = (minutes: number) => `${Math.round(minutes / 60)} ч`;
const planName = (plan: string | null | undefined) => (plan ? plan.charAt(0).toUpperCase() + plan.slice(1) : "");
const SUBSCRIPTIONS = new Set(["claudeai", "claude.ai", "chatgpt"]);

export function limitLabel(limit: Limit): string {
  if (limit.window === "session") return `Сессия · ${hours(limit.minutes ?? 300)}`;
  if (limit.window === "week") return limit.scope ? `Неделя · ${limit.scope}` : "Неделя";
  if (limit.minutes) return limit.minutes < 1440 ? hours(limit.minutes) : `${Math.round(limit.minutes / 1440)} дн`;
  return limit.scope ? `${limit.window} · ${limit.scope}` : limit.window;
}

function shortLabel(limit: Limit): string {
  if (limit.window === "session") return hours(limit.minutes ?? 300);
  return limit.window === "week" ? "нед." : limitLabel(limit);
}

function limitTone(percent: number, severity?: string | null): Tone {
  if (severity === "critical" || percent >= 90) return "danger";
  if (severity === "warning" || percent >= 75) return "warning";
  return "accent";
}

/** The time left until a reset, e.g. "3 ч 55 мин". */
export function until(seconds: number): string {
  if (seconds < 60) return "меньше минуты";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const total = Math.floor(minutes / 60);
  if (total < 24) return `${total} ч ${minutes % 60} мин`;
  return `${Math.floor(total / 24)} дн ${total % 24} ч`;
}

function ago(seconds: number): string {
  if (seconds < 60) return "только что";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч назад`;
  return `${Math.floor(seconds / 86400)} дн назад`;
}

// The moment of a reset in the user's time: "23:19" today, "вс, 8 нояб., 22:59" later.
function resetTime(at: number): string {
  const date = new Date(at * 1000);
  const today = date.toDateString() === new Date().toDateString();
  return date.toLocaleString(
    "ru-RU",
    today
      ? { hour: "2-digit", minute: "2-digit" }
      : { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" },
  );
}

// The agent's limits in a few characters (5 ч 4% · нед. 58%); the details on click.
function LimitsChip({ usage, expanded, onClick }: { usage: Usage | undefined; expanded: boolean; onClick: () => void }) {
  const rows = (usage?.limits ?? []).filter((l) => !l.scope).slice(0, 2);
  const worst = rows.reduce((max, l) => Math.max(max, l.percent), 0);
  const summary = rows.map((l) => `${shortLabel(l)} ${Math.round(l.percent)}%`).join(" · ");
  const spoken = rows.map((l) => `${limitLabel(l)} — использовано ${Math.round(l.percent)}%`).join(", ");
  return (
    <button
      type="button"
      className={`chat-tool-button chat-limits-chip tone-${rows.length > 0 ? limitTone(worst) : "neutral"}`}
      aria-expanded={expanded}
      aria-label={`Лимиты подписки${spoken ? `: ${spoken}` : ""}`}
      title="Лимиты подписки: сколько использовано и когда сброс"
      onClick={onClick}
    >
      <Gauge size={14} aria-hidden />
      {summary || "Лимиты"}
    </button>
  );
}

function AgentLimits({
  agent,
  auth,
  usage,
  now,
}: {
  agent: Agent;
  auth: AuthState | undefined;
  usage: Usage | undefined;
  now: number; // the daemon's clock
}) {
  const plan = planName(usage?.plan ?? auth?.plan);
  const subscription = SUBSCRIPTIONS.has(auth?.method ?? "");
  return (
    <section className="chat-limits-agent" aria-label={`Лимиты ${AGENTS[agent]}`}>
      <p className="chat-limits-name">
        {AGENTS[agent]}
        {plan ? ` · ${plan}` : ""}
      </p>
      {!usage ? (
        <p className="muted">Загрузка…</p>
      ) : usage.error ? (
        <p className="error">Не удалось узнать лимиты: {usage.error}</p>
      ) : usage.limits.length === 0 ? (
        <>
          <p className="muted">
            {subscription ? "CLI не сообщил лимиты подписки." : "Вход по API: лимитов подписки нет, оплата по использованию."}
          </p>
          {usage.text && <pre className="chat-limits-text">{usage.text}</pre>}
        </>
      ) : (
        usage.limits.map((limit, i) => {
          const percent = Math.max(0, Math.round(limit.percent));
          const label = limitLabel(limit);
          return (
            <div key={`${label}-${i}`} className="chat-limit">
              <div className="chat-limit-row">
                <span>{label}</span>
                <b>{percent}%</b>
              </div>
              <Progress value={Math.min(percent, 100)} tone={limitTone(percent, limit.severity)} label={`${label}: использовано ${percent}%`} />
              <small className="muted">
                {percent >= 100 ? "исчерпан" : `осталось ${100 - percent}%`}
                {limit.resets_at ? ` · сброс через ${until(limit.resets_at - now)} (${resetTime(limit.resets_at)})` : ""}
              </small>
            </div>
          );
        })
      )}
      {usage?.reached && <p className="error">Лимит исчерпан: агент продолжит после сброса.</p>}
      {usage && !usage.error && <small className="muted chat-limits-at">Обновлено {ago(now - usage.at)}</small>}
    </section>
  );
}

// A popover closes on a click outside it (and its button) or on Escape.
function useDismiss(open: boolean, onClose: () => void) {
  const box = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const down = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) close.current();
    };
    const key = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") close.current();
    };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [open]);
  return box;
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

// From this long, a running command gets a word that it is working, not stuck.
const LONG_ACTION_SECONDS = 60;

// The action of the current turn the agent waits for: a tool call with no result yet,
// timed from its start (after the user's permission when it needed one); the earliest of
// several. Events without a time (old history) are not timed.
export function runningAction(events: ChatEvent[]): { id: string; title: string; since: number } | null {
  let from = events.length;
  while (from > 0 && events[from - 1]?.t !== "user" && events[from - 1]?.t !== "turn") from -= 1;
  const since = new Map<string, number>();
  const titles = new Map<string, string>();
  const toolOf = new Map<string, string>(); // approval id -> tool call id
  for (const e of events.slice(from)) {
    const id = str(e.id);
    const ts = typeof e.ts === "number" ? e.ts : Number.NaN;
    if (e.t === "tool") {
      since.set(id, ts);
      titles.set(id, str(e.title));
    } else if (e.t === "approval" && e.tool) {
      toolOf.set(id, str(e.tool));
    } else if (e.t === "approval_done") {
      const tool = toolOf.get(id);
      if (tool === undefined) continue;
      if (e.decision === "allow" || e.decision === "allow_session") since.set(tool, ts);
      else since.delete(tool);
    } else if (e.t === "tool_result") {
      since.delete(id);
    }
  }
  let found: { id: string; title: string; since: number } | null = null;
  for (const [id, ts] of since)
    if (Number.isFinite(ts) && (!found || ts < found.since)) found = { id, title: titles.get(id) ?? "", since: ts };
  return found;
}

function duration(seconds: number): string {
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин ${seconds % 60} с`;
  return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
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
