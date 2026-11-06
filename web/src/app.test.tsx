import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, parseRoute } from "./App";
import { projectNameFrom } from "./NewProject";
import { RunTable } from "./ProjectPage";
import { buildTar, selectFolder } from "./tar";
import { findLinks } from "./terminalText";
import { ChatPanel } from "./ChatPanel";
import { queryClient } from "./data/queries";
import { useUi } from "./shell/store";

// A WebSocket double: the test plays the chat daemon.
class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  binaryType = "blob";
  constructor(public url: string) {
    FakeSocket.last = this;
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.();
    }, 0);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close() {
    this.readyState = 3;
  }
  emit(event: Record<string, unknown>) {
    act(() => this.onmessage?.({ data: JSON.stringify(event) }));
  }
}

// CodeMirror and xterm.js need a real layout engine: the browser test covers them.
vi.mock("./CodeEditor", () => ({
  default: (props: { path: string; content: string; readOnly: boolean; onChange: (t: string) => void; onSave: () => void }) => (
    <textarea
      aria-label={`editor ${props.path}`}
      defaultValue={props.content}
      readOnly={props.readOnly}
      onChange={(e) => props.onChange(e.target.value)}
    />
  ),
}));
vi.mock("./TerminalPanel", () => ({
  default: ({ projectId }: { projectId: string }) => <div>terminal of {projectId}</div>,
}));

type Handler = (path: string, init: RequestInit) => { status: number; body?: unknown };

function mockFetch(handler: Handler) {
  const calls: { path: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace("/api/v1", "");
      calls.push({ path, init });
      const { status, body } = handler(path, init);
      return new Response(body === undefined ? null : JSON.stringify(body), { status });
    }),
  );
  return calls;
}

type Reply = { status: number; body?: unknown };

// Routes keyed by "METHOD /path"; a function gets the parsed JSON body.
function serve(routes: Record<string, unknown>) {
  return mockFetch((path, init) => {
    const key = `${init.method ?? "GET"} ${path}`;
    if (!(key in routes)) return { status: 404, body: { error: { message: `no route ${key}` } } };
    const route = routes[key];
    if (typeof route === "function") {
      const json = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
      return (route as (body: unknown) => Reply)(json);
    }
    return { status: 200, body: route };
  });
}

function sent(calls: { path: string; init: RequestInit }[], method: string, path: string): unknown {
  const call = calls.find((c) => c.init.method === method && c.path === path);
  return call?.init.body ? JSON.parse(call.init.body as string) : undefined;
}

function signedIn(hash: string) {
  sessionStorage.setItem("ai-coding-token", "aic_session");
  window.location.hash = hash;
}

const ADMIN = { username: "root", is_admin: true, can_create_projects: true, token_id: "t-current" };
const USER = { username: "alice", is_admin: false, token_id: "t-alice" };
const CREATOR = { ...USER, can_create_projects: true };

function fileAt(relativePath: string, content: string): File {
  const file = new File([content], relativePath.split("/").pop() ?? "", {
    lastModified: 1_700_000_000_000,
  });
  Object.defineProperty(file, "webkitRelativePath", { value: relativePath });
  return file;
}

function blobBytes(blob: Blob): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

// A minimal pax/ustar reader: checks every header's checksum and pax record length.
async function readTar(blob: Blob): Promise<{ path: string; text: string }[]> {
  const data = await blobBytes(blob);
  const decoder = new TextDecoder();
  const out: { path: string; text: string }[] = [];
  let offset = 0;
  let paxPath: string | null = null;
  expect(data.length % 512).toBe(0);
  while (offset + 512 <= data.length) {
    const head = data.subarray(offset, offset + 512);
    if (head.every((b) => b === 0)) break;
    let sum = 0;
    head.forEach((b, i) => (sum += i >= 148 && i < 156 ? 32 : b));
    expect(parseInt(decoder.decode(head.subarray(148, 154)), 8)).toBe(sum);
    const size = parseInt(decoder.decode(head.subarray(124, 135)), 8);
    const type = String.fromCharCode(head[156] ?? 0);
    const body = data.subarray(offset + 512, offset + 512 + size);
    offset += 512 + Math.ceil(size / 512) * 512;
    if (type === "x") {
      const record = /^(\d+) path=([\s\S]*)\n$/.exec(decoder.decode(body));
      expect(Number(record?.[1])).toBe(body.length);
      paxPath = record?.[2] ?? null;
      continue;
    }
    out.push({ path: paxPath ?? "", text: decoder.decode(body) });
    paxPath = null;
  }
  return out;
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  queryClient.clear(); // server state must not leak between tests
  useUi.setState({ projectId: null, copilotOpen: false, paletteOpen: false, mobileNavOpen: false });
  window.location.hash = "";
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("routing", () => {
  it("parses hash routes", () => {
    expect(parseRoute("#/projects/p1")).toEqual({ view: "project", id: "p1" });
    expect(parseRoute("#/runs/r1")).toEqual({ view: "run", id: "r1" });
    expect(parseRoute("#/projects/p1/code")).toEqual({ view: "code", id: "p1" });
    expect(parseRoute("#/users")).toEqual({ view: "users" });
    expect(parseRoute("#/account")).toEqual({ view: "account" });
    expect(parseRoute("#/anything")).toEqual({ view: "dashboard" }); // Mission Control
    expect(parseRoute("")).toEqual({ view: "dashboard" });
    expect(parseRoute("#/projects")).toEqual({ view: "projects" });
    expect(parseRoute("#/projects/new")).toEqual({ view: "new" });
    expect(parseRoute("#/projects/p1/ai")).toEqual({ view: "ai", id: "p1" });
    expect(parseRoute("#/tasks?state=active")).toEqual({ view: "tasks", query: { state: "active" } });
    expect(parseRoute("#/settings/users")).toEqual({ view: "settings", tab: "users" });
    expect(parseRoute("#/projects/p1/code?terminal=claude%20auth%20login")).toEqual({
      view: "code",
      id: "p1",
      query: { terminal: "claude auth login" },
    });
  });

  it("shows the users section to administrators only", async () => {
    signedIn("#/users");
    serve({ "GET /me": USER, "GET /projects": [] });
    render(<App />);
    expect(await screen.findByText("Раздел доступен только администраторам.")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Пользователи" })).toBeNull();
  });
});

describe("projects", () => {
  it("derives a valid project name from a URL or a path", () => {
    expect(projectNameFrom("https://github.com/org/My Repo.git")).toBe("My-Repo");
    expect(projectNameFrom("git@github.com:org/service.git")).toBe("service");
    expect(projectNameFrom("/srv/repos/alpha/")).toBe("alpha");
  });

  it("packs long and non-ASCII paths into a valid tar", async () => {
    const long = `${"каталог/".repeat(30)}файл.txt`; // > 255 bytes: needs the pax record
    const { entries, folder } = selectFolder([fileAt(`proj/${long}`, "данные")]);
    expect(folder).toBe("proj");
    expect(await readTar(buildTar(entries))).toEqual([{ path: long, text: "данные" }]);
  });

  it("lets any user upload a project folder from their computer", async () => {
    signedIn("#/projects");
    const calls = serve({
      "GET /me": CREATOR,
      "GET /projects": [],
      "GET /project-sources": { git: true, upload_max_bytes: 1000, roots: [] },
      "POST /projects/upload?name=shop": () => ({ status: 201, body: { id: "p7", name: "shop" } }),
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Создать или подключить проект" }));
    // No allowed roots on the server: server directories are off and say why.
    expect(await screen.findByText(/AI_CODING_SERVER_ALLOWED_ROOTS/)).toBeTruthy();
    expect((screen.getByRole("radio", { name: /Папка на сервере/ }) as HTMLInputElement).disabled).toBe(true);
    const picker = screen.getByLabelText(/Папка проекта/) as HTMLInputElement;
    expect(picker.hasAttribute("webkitdirectory")).toBe(true);
    fireEvent.change(picker, {
      target: {
        files: [
          fileAt("shop/src/app.py", "print(1)\n"),
          fileAt("shop/node_modules/left-pad/index.js", "module.exports = 1"),
          fileAt("shop/.git/config", "[core]"),
          fileAt("shop/.DS_Store", "x"),
          fileAt("shop/.ai-coding/state.db", "state"),
          fileAt("shop/.ai-coding/config.yml", "version: 1\n"),
        ],
      },
    });
    expect(screen.getByText(/«shop»: 2 файл/).textContent).toContain("node_modules (1)");
    expect((screen.getByLabelText(/Имя проекта/) as HTMLInputElement).value).toBe("shop");
    fireEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    await waitFor(() => expect(window.location.hash).toBe("#/projects/p7"));
    const upload = calls.find((c) => c.path.startsWith("/projects/upload"));
    expect((upload?.init.headers as Record<string, string>)["Content-Type"]).toBe("application/x-tar");
    expect(await readTar(upload?.init.body as Blob)).toEqual([
      { path: ".ai-coding/config.yml", text: "version: 1\n" },
      { path: "src/app.py", text: "print(1)\n" },
    ]);
  });

  it("refuses a folder above the server's upload limit", async () => {
    signedIn("#/projects");
    serve({
      "GET /me": CREATOR,
      "GET /projects": [],
      "GET /project-sources": { git: true, upload_max_bytes: 10, roots: [] },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Создать или подключить проект" }));
    await screen.findByText(/AI_CODING_SERVER_ALLOWED_ROOTS/);
    fireEvent.change(screen.getByLabelText(/Папка проекта/), {
      target: { files: [fileAt("big/data.txt", "more than ten bytes")] },
    });
    expect((await screen.findByRole("alert")).textContent).toContain("больше допустимых");
    expect((screen.getByRole("button", { name: "Создать проект" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("clones a private Git repository with a token", async () => {
    signedIn("#/projects");
    const calls = serve({
      "GET /me": CREATOR,
      "GET /projects": [],
      "GET /project-sources": { git: true, upload_max_bytes: 1000, roots: [] },
      "POST /projects": () => ({ status: 201, body: { id: "p9", name: "service" } }),
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Создать или подключить проект" }));
    fireEvent.click(screen.getByRole("radio", { name: /Git-репозиторий/ }));
    fireEvent.change(screen.getByLabelText(/URL репозитория/), {
      target: { value: "https://github.com/org/service.git" },
    });
    fireEvent.change(screen.getByLabelText(/Токен доступа/), { target: { value: "github_pat_x" } });
    expect((screen.getByLabelText(/Имя проекта/) as HTMLInputElement).value).toBe("service");
    fireEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    await waitFor(() => expect(window.location.hash).toBe("#/projects/p9"));
    expect(sent(calls, "POST", "/projects")).toEqual({
      name: "service",
      git_url: "https://github.com/org/service.git",
      git_token: "github_pat_x",
    });
  });

  it("connects a free directory on the server and shows the server's error", async () => {
    signedIn("#/projects");
    let attempts = 0;
    const calls = serve({
      "GET /me": ADMIN,
      "GET /projects": [{ id: "p1", name: "beta", role: null, created_at: "2026-11-05T10:00:00" }],
      "GET /project-sources": {
        git: true,
        upload_max_bytes: 1000,
        roots: [{ name: "/srv/repos", path: "/srv/repos", git: false, used: true, project: null,
          directories: [
            { name: "alpha", path: "/srv/repos/alpha", git: true, used: false, project: null },
            { name: "beta", path: "/srv/repos/beta", git: false, used: true, project: "beta" },
            { name: "gamma", path: "/srv/repos/gamma", git: true, used: true, project: null },
          ] }],
      },
      "POST /projects": () =>
        ++attempts === 1
          ? { status: 409, body: { error: { message: "project 'alpha' already exists" } } }
          : { status: 201, body: { id: "p2", name: "alpha" } },
    });
    render(<App />);
    expect(await screen.findByText("нет доступа")).toBeTruthy(); // an admin who is not a member
    fireEvent.click(screen.getByRole("button", { name: "Создать или подключить проект" }));
    const radio = (await screen.findByRole("radio", { name: /Папка на сервере/ })) as HTMLInputElement;
    await waitFor(() => expect(radio.disabled).toBe(false));
    fireEvent.click(radio);
    expect((screen.getByRole("option", { name: /подключён: beta/ }) as HTMLOptionElement).disabled).toBe(true);
    // Another user's project: used, but its name is not shown.
    expect((screen.getByRole("option", { name: /gamma.*уже используется/ }) as HTMLOptionElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: /Каталог/ }), { target: { value: "/srv/repos/alpha" } });
    expect((screen.getByLabelText(/^Путь/) as HTMLInputElement).value).toBe("/srv/repos/alpha");
    fireEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    expect((await screen.findByRole("alert")).textContent).toContain("already exists");
    fireEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    await waitFor(() => expect(window.location.hash).toBe("#/projects/p2"));
    expect(sent(calls, "POST", "/projects")).toEqual({ name: "alpha", path: "/srv/repos/alpha" });
  });

  it("offers no project creation when the server reserves it to administrators", async () => {
    signedIn("#/projects");
    serve({ "GET /me": { ...USER, can_create_projects: false }, "GET /projects": [] });
    render(<App />);
    expect(await screen.findByText(/Попросите владельца проекта/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Создать или подключить проект" })).toBeNull();
  });

  it("deletes a project only after its name is typed", async () => {
    signedIn("#/projects/p1");
    const calls = serve({
      "GET /me": USER,
      "GET /projects/p1": { id: "p1", name: "alpha", role: "owner", created_at: "2026-11-05T10:00:00",
                            members: [{ username: "alice", role: "owner" }], kinds: ["audit"] },
      "GET /projects/p1/runs": [],
      "GET /projects/p1/findings": [],
      "GET /audit-events?project_id=p1": [],
      "DELETE /projects/p1": () => ({ status: 204 }),
      "GET /projects": [],
    });
    render(<App />);
    // Deletion lives on the owner's "Опасная зона" tab of the project.
    fireEvent.click(await screen.findByRole("tab", { name: "Опасная зона" }));
    fireEvent.click(await screen.findByRole("button", { name: "Удалить проект…" }));
    const confirm = screen.getByRole("button", { name: "Удалить навсегда" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/введите имя проекта/), { target: { value: "alpha" } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(window.location.hash).toBe("#/projects"));
    expect(calls.some((c) => c.init.method === "DELETE" && c.path === "/projects/p1")).toBe(true);
  });
});

describe("project explorer and onboarding", () => {
  const CONFIG = {
    stacks: ["python", "react"],
    mode: "develop",
    execution: { backend: "docker", network: "none" },
    ai: { enabled: false, provider: "none", model: null },
    security: { network: "local-only", egress_allowlist: [] },
    budgets: { max_tokens_per_run: 500000, max_cost_usd_per_run: 5, max_duration_minutes: 120 },
    quality_gates: { security: { enabled: true, blocking: true, fail_on: "high" } },
  };
  const detail = (id: string, name: string, role: string, stacks: string[]) => ({
    id, name, role, created_at: "2026-11-05T10:00:00", members: [{ username: "alice", role }], kinds: ["audit", "discover"],
    config: { ...CONFIG, stacks },
  });

  it("searches and filters projects; cards show the stack, last run and open findings", async () => {
    signedIn("#/projects");
    serve({
      "GET /me": USER,
      "GET /projects": [
        { id: "p1", name: "alpha", role: "owner", created_at: "2026-11-05T10:00:00" },
        { id: "p2", name: "beta", role: "developer", created_at: "2026-11-05T10:00:00" },
        { id: "p3", name: "gamma", role: null, created_at: "2026-11-05T10:00:00" },
      ],
      "GET /projects/p1": detail("p1", "alpha", "owner", ["python"]),
      "GET /projects/p2": detail("p2", "beta", "developer", ["go"]),
      "GET /runs?limit=100": [
        { id: "r1", project_id: "p1", project_name: "alpha", state: "FAILED", mode: "audit", goal: "audit",
          kind: "audit", requested_by: "alice", created_at: "2026-11-05T11:00:00", finished_at: null },
      ],
      "GET /findings?limit=1000": [
        { id: "f1", project_id: "p1", project_name: "alpha", source: "ruff", rule_id: "E1", severity: "low",
          file_path: "a.py", line: 1, title: "x", status: "open" },
        { id: "f2", project_id: "p1", project_name: "alpha", source: "ruff", rule_id: "E2", severity: "low",
          file_path: "a.py", line: 2, title: "y", status: "fixed" },
      ],
    });
    render(<App />);
    const alpha = (await screen.findByRole("link", { name: "alpha" })).closest("article") as HTMLElement;
    expect(await within(alpha).findByText("python")).toBeTruthy(); // stack from the configuration
    expect(await within(alpha).findByTitle("FAILED")).toBeTruthy(); // its last run
    expect(within(alpha).getByRole("link", { name: "1" }).getAttribute("href")).toBe("#/audit?project=p1"); // open only
    expect(screen.getByText("нет доступа")).toBeTruthy(); // gamma: no member, no actions
    fireEvent.change(screen.getByRole("searchbox", { name: "Поиск проектов" }), { target: { value: "go" } });
    expect(screen.queryByRole("link", { name: "alpha" })).toBeNull();
    expect(screen.getByRole("link", { name: "beta" })).toBeTruthy(); // matched by its stack
    fireEvent.change(screen.getByRole("searchbox", { name: "Поиск проектов" }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Роль" }), { target: { value: "owner" } });
    expect(screen.getByText("1 из 3")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "Список" }));
    expect(within(screen.getByRole("region", { name: "Проекты" })).getByRole("link", { name: "alpha" })).toBeTruthy();
  });

  it("summarises the project's configuration on its overview", async () => {
    signedIn("#/projects/p1");
    serve({
      "GET /me": USER,
      "GET /projects": [],
      "GET /projects/p1": detail("p1", "alpha", "developer", ["python", "react"]),
      "GET /projects/p1/runs": [],
      "GET /projects/p1/findings": [],
    });
    render(<App />);
    expect(await screen.findByText(/Private Mode/)).toBeTruthy();
    expect(screen.getByText("выключен")).toBeTruthy(); // the orchestrator has no AI provider
    expect(screen.getByText("react")).toBeTruthy();
    expect(screen.getByText("fail_on: high")).toBeTruthy();
    const main = screen.getByRole("main");
    expect(within(main).getByRole("link", { name: /AI Workspace/ }).getAttribute("href")).toBe("#/projects/p1/ai");
    expect(screen.queryByRole("tab", { name: "Опасная зона" })).toBeNull(); // not the owner
  });

  it("onboards a project: discovery run, its facts, then the five modes", async () => {
    signedIn("#/projects/new");
    const report = { run_id: "r1", sections: {
      "discover.stacks": { detected: ["python"], configured: [], effective: ["python"], missing_in_config: [] },
      "discover.git": { is_repository: true, branch: "main", head: "abcdef0123", clean: true, changed_files: 0 },
      "discover.context": { indexed_files: 42, indexed_bytes: 20480, top_extensions: { ".py": 30 } },
    } };
    const calls = serve({
      "GET /me": CREATOR,
      "GET /projects": [],
      "GET /project-sources": { git: true, upload_max_bytes: 1000, roots: [] },
      "POST /projects/upload?name=shop": () => ({ status: 201, body: { id: "p7", name: "shop" } }),
      "POST /projects/p7/runs": () => ({ status: 201, body: { id: "r1", state: "CREATED" } }),
      "GET /projects/p7": detail("p7", "shop", "owner", []),
      "GET /runs/r1": { id: "r1", state: "COMPLETED", mode: "discover", goal: "discover", error: null, kind: "discover",
                        project_id: "p7", approvals: [], jobs: [],
                        tasks: [{ key: "discover.stacks", phase: "DISCOVERY", status: "SUCCEEDED", attempts: 1, error: null }] },
      "GET /runs/r1/artifacts": [{ id: "a1", name: "discovery-report.json", sha256: "x", media_type: "application/json", size_bytes: 10 }],
      "GET /runs/r1/artifacts/a1": report,
    });
    render(<App />);
    expect((await screen.findByRole("list", { name: "Шаги подключения" })).textContent).toContain("Подключение");
    await screen.findByText(/AI_CODING_SERVER_ALLOWED_ROOTS/);
    fireEvent.change(screen.getByLabelText(/Папка проекта/), { target: { files: [fileAt("shop/app.py", "print(1)")] } });
    fireEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    expect(await screen.findByText(/Проект «shop» подключён/)).toBeTruthy();
    await waitFor(() => expect(sent(calls, "POST", "/projects/p7/runs")).toEqual({ kind: "discover", params: {} }));
    expect(await screen.findByText("42 файлов", { exact: false })).toBeTruthy(); // from the run's report
    expect(screen.getByText("python")).toBeTruthy();
    const modes = screen.getAllByRole("link").filter((a) => a.getAttribute("href")?.startsWith("#/projects/p7/ai?mode="));
    expect(modes.map((a) => a.getAttribute("href"))).toEqual(
      ["create", "develop", "modernize", "audit", "maintain"].map((m) => `#/projects/p7/ai?mode=${m}`),
    );
    expect(window.location.hash).toBe("#/projects/new"); // the wizard went on, it did not jump away
  });

  it("starts an AI task in the AI Workspace in the mode and with the draft it was given", async () => {
    signedIn("#/projects/p1/ai?mode=audit&draft=%D0%9F%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D1%8C%20auth");
    serve({
      "GET /me": USER,
      "GET /projects": [],
      "GET /projects/p1": detail("p1", "shop", "developer", ["python"]),
      "GET /projects/p1/workspace": { git: true, exists: true, can_edit: true, terminal: "on", branch_prefix: "ws/alice/" },
      "GET /projects/p1/files?path=&scope=workspace": [{ name: "app.py", type: "file", size: 6 }],
      "GET /runs?limit=100&project_id=p1": [],
    });
    render(<App />);
    expect(await screen.findByRole("tab", { name: /Chat/ })).toBeTruthy();
    await waitFor(() => expect(FakeSocket.last?.sent.length).toBe(2));
    const socket = FakeSocket.last as FakeSocket;
    socket.emit({ t: "hello", sessions: [{ id: "s9", agent: "claude", mode: "ask", workflow: "develop", title: "Старый чат" }] });
    expect(socket.sent.some((m) => (m as { op?: string }).op === "open")).toBe(false); // a new task, not the old chat
    const box = screen.getByLabelText("Сообщение") as HTMLTextAreaElement;
    expect(box.value).toBe("Проверь auth"); // pre-filled, not sent
    expect(screen.getByRole("radio", { name: /^Аудит/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(socket.sent.at(-1)).toEqual({ op: "new", agent: "claude", mode: "ask", workflow: "audit" });
    // The other centre tabs show the orchestrator's real data (none yet).
    fireEvent.click(screen.getByRole("tab", { name: /Tests/ }));
    expect(await screen.findByText("Аудитов ещё не было")).toBeTruthy();
  });
});

describe("users", () => {
  const users = [
    { id: "u1", username: "root", display_name: "Root Admin", is_admin: true, is_active: true,
      created_at: "2026-11-05T10:00:00" },
    { id: "u2", username: "bob", display_name: "Bob", is_admin: false, is_active: true,
      created_at: "2026-11-05T11:00:00" },
  ];

  it("blocks a user, and never offers to block or demote oneself", async () => {
    signedIn("#/users");
    const calls = serve({
      "GET /me": ADMIN,
      "GET /users": users,
      "PATCH /users/bob": (body: unknown) => ({ status: 200, body: { ...users[1], ...(body as object) } }),
    });
    render(<App />);
    const self = (await screen.findByText("root", { selector: "td" })).closest("tr") as HTMLElement;
    expect(within(self).queryByRole("button")).toBeNull();
    const bob = screen.getByText("bob").closest("tr") as HTMLElement;
    fireEvent.click(within(bob).getByRole("button", { name: "Заблокировать" }));
    expect((await screen.findByRole("status")).textContent).toContain("bob заблокирован");
    expect(sent(calls, "PATCH", "/users/bob")).toEqual({ is_active: false });
  });

  it("creates a user after checking the repeated password", async () => {
    signedIn("#/users");
    const calls = serve({
      "GET /me": ADMIN,
      "GET /users": users,
      "POST /users": () => ({ status: 201, body: { id: "u3", username: "carol" } }),
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Добавить пользователя" }));
    const form = screen.getByRole("form", { name: "Новый пользователь" });
    fireEvent.change(within(form).getByLabelText("Логин"), { target: { value: "carol" } });
    fireEvent.change(within(form).getByLabelText(/^Пароль/), { target: { value: "long password 1" } });
    fireEvent.change(within(form).getByLabelText("Повторите пароль"), { target: { value: "other" } });
    fireEvent.click(within(form).getByRole("checkbox"));
    fireEvent.submit(form);
    expect((await within(form).findByRole("alert")).textContent).toBe("Пароли не совпадают");
    expect(sent(calls, "POST", "/users")).toBeUndefined();
    fireEvent.change(within(form).getByLabelText("Повторите пароль"), {
      target: { value: "long password 1" },
    });
    fireEvent.submit(form);
    expect((await screen.findByRole("status")).textContent).toContain("carol создан");
    expect(sent(calls, "POST", "/users")).toEqual({
      username: "carol",
      password: "long password 1",
      is_admin: true,
    });
  });
});

describe("account", () => {
  it("changes the password and issues a token shown once", async () => {
    signedIn("#/account");
    const calls = serve({
      "GET /me": USER,
      "GET /projects": [{ id: "p1", name: "alpha", role: "developer", created_at: "2026-11-05T10:00:00" }],
      "GET /tokens": [
        { id: "t-alice", name: "login", project_id: null, max_role: null, revoked: false,
          created_at: "2026-11-05T10:00:00", expires_at: "2999-01-01T00:00:00", last_used_at: null },
        { id: "t-old", name: "old", project_id: null, max_role: null, revoked: true,
          created_at: "2026-11-01T10:00:00", expires_at: null, last_used_at: null },
      ],
      "PUT /me/password": () => ({ status: 204 }),
      "POST /tokens": () => ({ status: 201, body: { token: "aic_new_secret", token_id: "t9" } }),
    });
    render(<App />);
    const password = await screen.findByRole("form", { name: "Смена пароля" });
    fireEvent.change(within(password).getByLabelText("Текущий пароль"), { target: { value: "old one" } });
    fireEvent.change(within(password).getByLabelText(/^Новый пароль/), { target: { value: "new password 12" } });
    fireEvent.change(within(password).getByLabelText("Повторите пароль"), { target: { value: "new password 12" } });
    fireEvent.submit(password);
    expect((await within(password).findByRole("status")).textContent).toContain("Пароль изменён");
    expect(sent(calls, "PUT", "/me/password")).toEqual({
      current_password: "old one",
      new_password: "new password 12",
    });

    // The session's own token is marked; revoked tokens are hidden by default.
    expect(await screen.findByText("текущая сессия")).toBeTruthy();
    expect(screen.queryByText("old")).toBeNull();

    const issue = screen.getByRole("form", { name: "Новый токен" });
    fireEvent.change(within(issue).getByLabelText("Название"), { target: { value: "ci" } });
    fireEvent.change(within(issue).getByLabelText("Проект"), { target: { value: "p1" } });
    fireEvent.change(within(issue).getByLabelText("Макс. роль"), { target: { value: "auditor" } });
    fireEvent.submit(issue);
    expect(((await screen.findByLabelText("Новый токен", { selector: "input" })) as HTMLInputElement).value)
      .toBe("aic_new_secret");
    expect(sent(calls, "POST", "/tokens")).toEqual({ name: "ci", project_id: "p1", max_role: "auditor" });
  });
});

describe("login", () => {
  it("logs in, stores the token for the session and sends it as a bearer token", async () => {
    const calls = mockFetch((path) => {
      if (path === "/auth/login") return { status: 201, body: { token: "aic_test" } };
      if (path === "/me") return { status: 200, body: { username: "alice", is_admin: false } };
      if (path === "/projects")
        return {
          status: 200,
          body: [{ id: "p1", name: "alpha", role: "developer", created_at: "2026-11-05T10:00:00" }],
        };
      return { status: 404, body: { error: { message: "not found" } } };
    });
    render(<App />);
    fireEvent.change(screen.getByLabelText("Пользователь"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "secret password" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    // Mission Control greets the user; the shell lists their projects.
    // Mission Control is a lazy chunk: allow for a loaded machine.
    expect(await screen.findByRole("heading", { name: /alice/ }, { timeout: 5000 })).toBeTruthy();
    await waitFor(() => expect(calls.some((c) => c.path === "/projects")).toBe(true));
    expect(sessionStorage.getItem("ai-coding-token")).toBe("aic_test");
    const projects = calls.find((c) => c.path === "/projects");
    expect((projects?.init.headers as Record<string, string>).Authorization).toBe("Bearer aic_test");
    expect(projects?.init.credentials).toBe("omit");
  });

  it("shows the server's error and keeps no token", async () => {
    mockFetch(() => ({ status: 401, body: { error: { message: "invalid username or password" } } }));
    render(<App />);
    fireEvent.change(screen.getByLabelText("Пользователь"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "y" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect((await screen.findByRole("alert")).textContent).toContain("invalid username");
    expect(sessionStorage.getItem("ai-coding-token")).toBeNull();
  });

  it("drops an expired token and returns to the login form", async () => {
    sessionStorage.setItem("ai-coding-token", "aic_old");
    mockFetch(() => ({ status: 401, body: { error: { message: "missing or invalid API token" } } }));
    render(<App />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Войти" })).toBeTruthy());
    expect(sessionStorage.getItem("ai-coding-token")).toBeNull();
  });
});

describe("runs table", () => {
  it("renders states and links", () => {
    render(
      <RunTable
        runs={[
          { id: "01a1-0000-000000000001", state: "COMPLETED", mode: "audit", goal: "audit",
            kind: "audit", requested_by: "alice", created_at: "2026-11-05T10:00:00", finished_at: null },
        ]}
      />,
    );
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe("#/runs/01a1-0000-000000000001");
    const state = screen.getByTitle("COMPLETED"); // the state badge: label and tone
    expect(state.textContent).toBe("Завершён");
    expect(state.className).toContain("tone-success");
  });
});

describe("workspace", () => {
  const project = { id: "p1", name: "shop", role: "developer", created_at: "2026-11-05T10:00:00",
                    members: [], kinds: ["audit"] };
  const info = { git: true, exists: false, can_edit: true, terminal: "on", branch_prefix: "ws/alice/" };

  it("shows the project read-only, then opens the user's own workspace", async () => {
    signedIn("#/projects/p1/code");
    let exists = false;
    const calls = serve({
      "GET /me": USER,
      "GET /projects/p1": project,
      "GET /projects/p1/workspace": () => ({ status: 200, body: { ...info, exists } }),
      "POST /projects/p1/workspace": () => {
        exists = true;
        return { status: 201, body: { ...info, exists } };
      },
      "GET /projects/p1/files?path=&scope=project": [{ name: "README.md", type: "file", size: 7 }],
      "GET /projects/p1/files?path=&scope=workspace": [
        { name: "src", type: "dir", size: 0 },
        { name: "README.md", type: "file", size: 7 },
      ],
      "GET /projects/p1/files?path=src&scope=workspace": [{ name: "app.py", type: "file", size: 6 }],
      "GET /projects/p1/workspace/git": { branch: "main", files: [] },
    });
    render(<App />);
    expect(await screen.findByText("файлы проекта, только чтение")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "README.md" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Открыть мою рабочую область" }));
    expect(await screen.findByText("моя рабочая область")).toBeTruthy();
    expect(calls.some((c) => c.init.method === "POST" && c.path === "/projects/p1/workspace")).toBe(true);
    fireEvent.click(await screen.findByRole("button", { name: /src/ }));
    expect(await screen.findByRole("button", { name: "app.py" })).toBeTruthy();
    expect(await screen.findByText("Изменений нет.")).toBeTruthy();
    expect(screen.getByLabelText("Сообщение")).toBeTruthy(); // the AI chat is open by default
    fireEvent.click(screen.getByRole("button", { name: "Терминал" }));
    expect(await screen.findByText("terminal of p1")).toBeTruthy();
  });

  it("edits, saves with the loaded version and reports a conflict", async () => {
    signedIn("#/projects/p1/code");
    let saves = 0;
    const calls = serve({
      "GET /me": USER,
      "GET /projects/p1": project,
      "GET /projects/p1/workspace": { ...info, exists: true },
      "GET /projects/p1/files?path=&scope=workspace": [{ name: "app.py", type: "file", size: 6 }],
      "GET /projects/p1/file?path=app.py&scope=workspace": {
        path: "app.py", size: 6, content: "x = 1\n", binary: false, too_large: false, version: "v1",
      },
      "PUT /projects/p1/file?path=app.py": () =>
        ++saves === 1
          ? { status: 200, body: { path: "app.py", version: "v2" } }
          : { status: 409, body: { error: { message: "the file changed since it was opened" } } },
      "PUT /projects/p1/file?path=docs%2Fnew.md": () => ({ status: 200, body: { path: "docs/new.md", version: "v0" } }),
      "GET /projects/p1/file?path=docs%2Fnew.md&scope=workspace": {
        path: "docs/new.md", size: 0, content: "", binary: false, too_large: false, version: "v0",
      },
      "GET /projects/p1/workspace/git": { branch: "main", files: [{ status: " M", path: "app.py" }] },
      "POST /projects/p1/workspace/commit": () => ({ status: 200, body: { ok: true, output: "", branch: "main" } }),
      "POST /projects/p1/workspace/publish": () => ({ status: 200, body: { branch: "ws/alice/main", commit: "abcdef0123" } }),
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "app.py" }));
    const editor = await screen.findByLabelText("editor app.py");
    fireEvent.change(editor, { target: { value: "x = 2\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await screen.findByText("сохранено");
    expect(sent(calls, "PUT", "/projects/p1/file?path=app.py")).toEqual({ content: "x = 2\n", version: "v1", create: false });
    fireEvent.change(editor, { target: { value: "x = 3\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Файл изменился");

    // A new file is created only if it does not exist yet.
    fireEvent.change(screen.getByPlaceholderText(/новый файл/), { target: { value: "docs/new.md" } });
    fireEvent.click(screen.getByRole("button", { name: "+" }));
    expect(await screen.findByLabelText("editor docs/new.md")).toBeTruthy();
    expect(sent(calls, "PUT", "/projects/p1/file?path=docs%2Fnew.md")).toEqual({ content: "", version: null, create: true });

    // Git: changed files, commit, publish to the project.
    expect(await screen.findByText("app.py", { selector: "li" })).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Сообщение коммита"), { target: { value: "x = 2" } });
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать" }));
    await waitFor(() => expect(sent(calls, "POST", "/projects/p1/workspace/commit")).toEqual({ message: "x = 2" }));
    fireEvent.click(screen.getByRole("button", { name: "Отправить в проект" }));
    expect(await screen.findByText(/ветка ws\/alice\/main \(abcdef01\)/)).toBeTruthy();
  });
});

describe("chat", () => {
  async function connected(props: Partial<Parameters<typeof ChatPanel>[0]> = {}) {
    sessionStorage.setItem("ai-coding-token", "aic_session");
    const onOpenTerminal = vi.fn();
    const onFilesChanged = vi.fn();
    render(<ChatPanel projectId="p1" onOpenTerminal={onOpenTerminal} onFilesChanged={onFilesChanged} {...props} />);
    await waitFor(() => expect(FakeSocket.last?.sent.length).toBe(2));
    const socket = FakeSocket.last as FakeSocket;
    expect(socket.url).toContain("/api/v1/projects/p1/workspace/chat");
    expect(socket.sent).toEqual([{ token: "aic_session" }, { op: "hello" }]);
    return { socket, onOpenTerminal, onFilesChanged };
  }

  it("runs a turn: streaming, tool cards, a permission, the result", async () => {
    const { socket, onFilesChanged } = await connected();
    socket.emit({ t: "hello", sessions: [] });
    socket.emit({ t: "auth", agents: { claude: { logged_in: true, method: "claudeai" },
                                       codex: { logged_in: false, method: null } } });
    expect(screen.getByText(/Claude Code · подписка Claude/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "Добавь функцию tax" } });
    fireEvent.keyDown(screen.getByLabelText("Сообщение"), { key: "Enter" });
    expect(socket.sent.at(-1)).toEqual({ op: "new", agent: "claude", mode: "ask", workflow: "develop" });
    socket.emit({ t: "opened", meta: { id: "s1", agent: "claude", mode: "ask", workflow: "develop", title: "Новый чат" }, history: [] });
    expect(socket.sent.at(-1)).toEqual({ op: "send", session: "s1", text: "Добавь функцию tax", mode: "ask" });
    const s = { session: "s1" };
    socket.emit({ ...s, t: "user", text: "Добавь функцию tax" });
    socket.emit({ ...s, t: "turn", state: "running" });
    expect(screen.getByRole("button", { name: "Остановить" })).toBeTruthy();
    socket.emit({ ...s, t: "delta", msg: "m1", text: "Сейчас " });
    socket.emit({ ...s, t: "delta", msg: "m1", text: "сделаю" });
    expect(screen.getByText("Сейчас сделаю")).toBeTruthy();
    socket.emit({ ...s, t: "message", msg: "m1", text: "Сейчас сделаю `tax()`" });
    expect(screen.getByText("tax()", { selector: "code" })).toBeTruthy();
    socket.emit({ ...s, t: "tool", id: "t1", name: "Edit", title: "Изменить shop.py", detail: "- a\n+ b" });
    socket.emit({ ...s, t: "approval", id: "a1", name: "Edit", title: "Изменить shop.py", detail: "", tool: "t1", session_option: true });
    expect(screen.getByText(/Нужно разрешение/)).toBeTruthy();
    // Inside the tool's card, as in VS Code: the action is shown once.
    expect(screen.getAllByText("Изменить shop.py")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Разрешить в этой сессии" }));
    expect(socket.sent.at(-1)).toEqual({ op: "approve", session: "s1", id: "a1", decision: "allow_session" });
    socket.emit({ ...s, t: "approval_done", id: "a1", decision: "allow_session" });
    socket.emit({ ...s, t: "tool_result", id: "t1", ok: true, diff: "@@ -1 +1 @@\n-a\n+b", file: "shop.py" });
    expect(onFilesChanged).toHaveBeenCalled();
    // Answered: the actions fold into one line, as in VS Code; details on demand.
    expect(screen.queryByText("+b")).toBeNull();
    const head = screen.getByRole("button", { name: /Действия: 1/ });
    expect(head.textContent).toContain("изменено: shop.py");
    fireEvent.click(head);
    expect(screen.getByText("Разрешено до конца сессии")).toBeTruthy();
    fireEvent.click(screen.getByText("Изменить shop.py"));
    expect(screen.getByText("+b").className).toBe("add");
    socket.emit({ ...s, t: "turn", state: "done", duration_ms: 1500, cost: 0.0123 });
    expect(screen.getByText(/Готово за 1.5 с · \$0.0123/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeTruthy();
    // Events of another chat do not leak into this one.
    socket.emit({ session: "other", t: "message", msg: "x", text: "чужое" });
    expect(screen.queryByText("чужое")).toBeNull();
  });

  it("reopens the latest chat with its history, and stops a running agent", async () => {
    const { socket } = await connected();
    socket.emit({ t: "hello", sessions: [{ id: "s9", agent: "codex", mode: "ask", title: "Старый чат" }] });
    expect(socket.sent.at(-1)).toEqual({ op: "open", session: "s9" });
    socket.emit({
      t: "opened",
      busy: true,
      meta: { id: "s9", agent: "codex", mode: "full", title: "Старый чат", model: "gpt-x" },
      history: [
        { session: "s9", t: "user", text: "Почини тесты" },
        { session: "s9", t: "tool", id: "c1", title: "$ pytest", detail: "" },
        { session: "s9", t: "tool_result", id: "c1", ok: false, text: "1 failed" },
      ],
    });
    expect(screen.getByText("Почини тесты")).toBeTruthy();
    expect(screen.queryByText("1 failed")).toBeNull(); // folded
    const head = screen.getByRole("button", { name: /Действия: 1/ });
    expect(head.textContent).toContain("с ошибкой: 1");
    expect(head.textContent).toContain("$ pytest"); // the agent is still at it: its current action
    fireEvent.click(head);
    fireEvent.click(screen.getByText("$ pytest"));
    expect(screen.getByText("1 failed")).toBeTruthy();
    expect((screen.getByRole("tab", { name: "Codex" }) as HTMLButtonElement).getAttribute("aria-selected")).toBe("true");
    expect((screen.getByLabelText("Разрешения") as HTMLSelectElement).value).toBe("full");
    fireEvent.click(screen.getByRole("button", { name: "Остановить" }));
    expect(socket.sent.at(-1)).toEqual({ op: "interrupt", session: "s9" });
  });

  it("works in the mode chosen for the chat: AUDIT only reads", async () => {
    const { socket } = await connected();
    socket.emit({ t: "hello", sessions: [] });
    expect(screen.getByRole("radio", { name: /^Разработка/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("radio", { name: /^Аудит/ }));
    expect(screen.queryByLabelText("Разрешения")).toBeNull();
    expect(screen.getByText("Только чтение")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "Проверь безопасность" } });
    fireEvent.keyDown(screen.getByLabelText("Сообщение"), { key: "Enter" });
    expect(socket.sent.at(-1)).toEqual({ op: "new", agent: "claude", mode: "ask", workflow: "audit" });
    socket.emit({ t: "opened", meta: { id: "s1", agent: "claude", mode: "ask", workflow: "audit", title: "Новый чат" }, history: [] });
    expect(screen.getByText(/только чтение$/, { selector: "p" })).toBeTruthy();
    expect(screen.queryByRole("radio")).toBeNull(); // an open chat keeps its mode
    const s = { session: "s1" };
    socket.emit({ ...s, t: "tool", id: "t1", name: "Bash", title: "$ rm -rf build", detail: "" });
    socket.emit({ ...s, t: "notice", text: "Режим «Аудит» только читает: «$ rm -rf build» не выполнено." });
    socket.emit({ ...s, t: "tool_result", id: "t1", ok: false, text: "denied" });
    const head = screen.getByRole("button", { name: /Действия: 1/ });
    expect(head.textContent).toContain("отклонено (только чтение): 1");
    fireEvent.click(head);
    expect(screen.getByText(/«\$ rm -rf build» не выполнено/)).toBeTruthy();
  });

  it("does not let a workspace older than the modes work without one", async () => {
    const { socket } = await connected();
    socket.emit({ t: "hello", sessions: [] });
    fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "Проверь код" } });
    fireEvent.keyDown(screen.getByLabelText("Сообщение"), { key: "Enter" });
    // The old chat service ignores "workflow": its chat comes back without one.
    socket.emit({ t: "opened", meta: { id: "s1", agent: "claude", mode: "ask", title: "Новый чат" }, history: [] });
    expect(screen.getByRole("alert").textContent).toContain("старого образа");
    expect(socket.sent.some((m) => (m as { op?: string }).op === "send")).toBe(false);
    expect((screen.getByLabelText("Сообщение") as HTMLTextAreaElement).value).toBe("Проверь код");
  });

  it("changes code only after the plan is approved", async () => {
    const { socket } = await connected();
    socket.emit({ t: "hello", sessions: [] });
    fireEvent.click(screen.getByRole("radio", { name: /^Сопровождение/ }));
    fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "Почини оплату" } });
    fireEvent.keyDown(screen.getByLabelText("Сообщение"), { key: "Enter" });
    expect(socket.sent.at(-1)).toEqual({ op: "new", agent: "claude", mode: "ask", workflow: "maintain" });
    const meta = { id: "s1", agent: "claude", mode: "edits", workflow: "maintain", title: "Почини оплату" };
    const s = { session: "s1" };
    socket.emit({ t: "opened", meta, history: [{ ...s, t: "user", text: "Почини оплату" }] });
    expect(screen.getByText(/сначала план/)).toBeTruthy();
    // Claude Code's plan mode: the plan is a report of its own, with its decision.
    socket.emit({ ...s, t: "tool", id: "p1", name: "ExitPlanMode", title: "План работы", detail: "" });
    socket.emit({ ...s, t: "approval", id: "a1", name: "ExitPlanMode", title: "План работы",
                  detail: "# План\n1. **Тест** на ошибку\n2. Исправить", tool: "p1" });
    expect(screen.getByText("План работы")).toBeTruthy();
    expect(screen.getByText("Тест", { selector: "b" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Действия/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Утвердить план и выполнить" })).toBeNull(); // asking already
    fireEvent.click(screen.getByRole("button", { name: "Утвердить план" }));
    expect(socket.sent.at(-1)).toEqual({ op: "approve", session: "s1", id: "a1", decision: "allow" });
    socket.emit({ ...s, t: "planned" });
    socket.emit({ ...s, t: "approval_done", id: "a1", decision: "allow" });
    socket.emit({ ...s, t: "tool_result", id: "p1", ok: true, text: "approved" });
    expect(screen.getByText("✓ План утверждён")).toBeTruthy();
    expect(screen.getByText(/план утверждён$/, { selector: "p" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Действия/ })).toBeNull();

    // Codex has no plan mode: its plan is a message, approved from the chat.
    fireEvent.click(screen.getByRole("tab", { name: "Codex" }));
    fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "Обнови зависимости" } });
    fireEvent.keyDown(screen.getByLabelText("Сообщение"), { key: "Enter" });
    expect(socket.sent.at(-1)).toEqual({ op: "new", agent: "codex", mode: "edits", workflow: "maintain" });
    const c = { session: "s2" };
    socket.emit({ t: "opened", meta: { ...meta, id: "s2", agent: "codex" }, history: [
      { ...c, t: "user", text: "Обнови зависимости" },
      { ...c, t: "message", msg: "m", text: "План: обновить requests" },
      { ...c, t: "turn", state: "done" },
    ] });
    fireEvent.click(screen.getByRole("button", { name: "Утвердить план и выполнить" }));
    expect(socket.sent.at(-1)).toEqual({ op: "send", session: "s2", text: "План утверждён — выполняй.", mode: "edits",
                                         approve_plan: true });
    socket.emit({ ...c, t: "planned" });
    expect(screen.queryByRole("button", { name: "Утвердить план и выполнить" })).toBeNull();
  });

  it("links the platform runs the agent started", async () => {
    const { socket } = await connected();
    socket.emit({ t: "hello", sessions: [] });
    socket.emit({ t: "opened", meta: { id: "s1", agent: "claude", mode: "ask", workflow: "audit", title: "Аудит" }, history: [] });
    const run = "0f3c2a1e-1111-4222-8333-444455556666";
    socket.emit({ session: "s1", t: "message", msg: "m", text: `Аудит платформы: #/runs/${run}, затем \`#/runs/${run}\`` });
    const links = screen.getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual([`#/runs/${run}`, `#/runs/${run}`]);
    expect(links[0]?.getAttribute("target")).toBe("_blank");
  });

  it("reconnects by itself and follows the open chat again", async () => {
    const { socket } = await connected();
    socket.emit({ t: "hello", sessions: [] });
    socket.emit({ t: "opened", meta: { id: "s1", agent: "claude", mode: "ask", title: "Чат" }, history: [] });
    act(() => socket.onclose?.());
    await waitFor(() => expect(FakeSocket.last).not.toBe(socket), { timeout: 3000 });
    const again = FakeSocket.last as FakeSocket;
    await waitFor(() => expect(again.sent).toContainEqual({ op: "open", session: "s1" }));
  });

  it("signs in: Codex by a ChatGPT device code, Claude Code in the terminal", async () => {
    const { socket, onOpenTerminal } = await connected();
    socket.emit({ t: "hello", sessions: [] });
    socket.emit({ t: "auth", agents: { claude: { logged_in: false, method: null }, codex: { logged_in: false, method: null } } });
    fireEvent.click(screen.getByRole("button", { name: "Войти в терминале" }));
    expect(onOpenTerminal).toHaveBeenCalledWith("claude auth login");
    fireEvent.click(screen.getByRole("tab", { name: "Codex" }));
    fireEvent.click(screen.getByRole("button", { name: "Войти через ChatGPT" }));
    expect(socket.sent.at(-1)).toEqual({ op: "login", agent: "codex" });
    socket.emit({ t: "login", agent: "codex", url: "https://auth.openai.com/codex/device", code: "ABCD-1234" });
    expect(screen.getByText("ABCD-1234")).toBeTruthy();
    socket.emit({ t: "login_done", agent: "codex", success: true });
    expect(screen.getByText("Вход выполнен.")).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Только план" })).toBeNull(); // Claude Code only
  });
});

describe("terminal", () => {
  it("rejoins a sign-in link that tmux broke across rows", () => {
    const url =
      "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&state=CcgoH93UHQt7";
    const cols = 40;
    const text = `If the browser didn't open, visit: ${url}`;
    const rows: string[] = [];
    for (let i = 0; i < text.length; i += cols) rows.push(text.slice(i, i + cols));
    rows.push("Paste code here if prompted >");
    expect(findLinks(rows, cols)).toEqual([url]);
    expect(findLinks(["see https://example.org/a.", "next line"], 80)).toEqual(["https://example.org/a"]);
    expect(findLinks(["no links here"], 80)).toEqual([]);
  });
});
