import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentExecution, RunDetail } from "../api";
import { queryClient } from "../data/queries";
import { RunPage } from "../RunPage";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
import Agents, { agentStatus } from "./Agents";
import Tasks, { parseIssues } from "./Tasks";

// React Flow needs a real layout engine: the graph is checked by its inputs here.
vi.mock("./RunGraph", () => ({
  default: (p: { tasks: unknown[]; dependencies: unknown[]; annotations?: Record<string, { label: string }[]> }) => (
    <div>
      graph: {p.tasks.length} steps, {p.dependencies.length} links
      {Object.entries(p.annotations ?? {}).map(([k, v]) => (
        <span key={k}>
          {k}={v.map((a) => a.label).join(",")}
        </span>
      ))}
    </div>
  ),
}));

type Reply = { status: number; body?: unknown };
function serve(routes: Record<string, unknown>) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace("/api/v1", "");
      const method = init.method ?? "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ method, path, body });
      const key = `${method} ${path}`;
      if (!(key in routes)) return new Response(JSON.stringify({ error: { message: `no route ${key}` } }), { status: 404 });
      const route = routes[key];
      const { status, body: out } = typeof route === "function" ? (route as (b: unknown) => Reply)(body) : { status: 200, body: route };
      return new Response(out === undefined ? null : JSON.stringify(out), { status });
    }),
  );
  return calls;
}

function show(node: ReactNode) {
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>{node}</TooltipProvider>
    </QueryClientProvider>,
  );
}

const PROJECTS = [
  { id: "p1", name: "alpha", role: "owner", created_at: "2026-11-01T10:00:00Z" },
  { id: "p2", name: "beta", role: "developer", created_at: "2026-11-02T10:00:00Z" },
];
const run = (id: string, state: string, kind: string, project: string, goal: string) => ({
  id,
  state,
  kind,
  mode: kind,
  goal,
  project_id: project,
  project_name: project === "p1" ? "alpha" : "beta",
  requested_by: "alice",
  created_at: "2026-11-06T10:00:00Z",
  finished_at: state === "RUNNING" || state === "AWAITING_APPROVAL" ? null : "2026-11-06T10:02:00Z",
});
const RUNS = [
  run("r1", "COMPLETED", "audit", "p1", "audit: the project"),
  run("r2", "AWAITING_APPROVAL", "maintain", "p2", "Fix login"),
  run("r3", "FAILED", "develop", "p1", "develop: Add tax"),
];

beforeEach(() => {
  sessionStorage.setItem("ai-coding-token", "aic_session");
  queryClient.clear();
  useUi.setState({ projectId: null, copilotOpen: false, paletteOpen: false });
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("tasks", () => {
  it("filters runs and decides from the approval queue", async () => {
    const calls = serve({
      "GET /projects": PROJECTS,
      "GET /runs?limit=100": RUNS,
      "GET /runs?limit=100&project_id=p2": [RUNS[1]],
      "GET /runs/r2": {
        ...RUNS[1],
        error: null,
        tasks: [],
        jobs: [],
        approvals: [{ id: "a1", operation: "merge", status: "pending", reason: "needs a person", decided_by: null }],
      },
      "POST /runs/r2/approve": { state: "RUNNING" },
    });
    show(<Tasks query={{}} />);
    expect(await screen.findByText("develop: Add tax")).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Ждут согласия/ }).textContent).toContain("1");
    expect(screen.getByRole("tab", { name: /С ошибкой/ }).textContent).toContain("1");

    fireEvent.change(screen.getByLabelText("Поиск задач"), { target: { value: "fix" } });
    expect(screen.queryByText("develop: Add tax")).toBeNull();
    expect(screen.getByText("Fix login")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Поиск задач"), { target: { value: "" } });

    fireEvent.change(screen.getByLabelText("Проект"), { target: { value: "p2" } });
    await waitFor(() => expect(calls.some((c) => c.path === "/runs?limit=100&project_id=p2")).toBe(true));

    fireEvent.click(screen.getByRole("tab", { name: /Ждут согласия/ }));
    expect(await screen.findByText("merge")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Комментарий: Fix login"), { target: { value: "ok" } });
    fireEvent.click(screen.getByRole("button", { name: "Одобрить" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toEqual({ method: "POST", path: "/runs/r2/approve", body: { approve: true, note: "ok" } }));
  });

  it("opens on the approval queue from a link", async () => {
    serve({ "GET /projects": PROJECTS, "GET /runs?limit=100": [], "GET /runs/r2": {} });
    show(<Tasks query={{ tab: "approvals" }} />);
    expect(await screen.findByText("Ничего не ждёт согласия")).toBeTruthy();
  });

  it("starts a task and shows the server's refusal", async () => {
    const calls = serve({
      "GET /projects": PROJECTS,
      "GET /runs?limit=100": [],
      "POST /projects/p1/runs": (body: unknown) =>
        (body as { kind: string }).kind === "develop"
          ? { status: 400, body: { error: { message: "the project has no AI provider for the orchestrator's agents" } } }
          : { status: 201, body: { id: "r9", state: "CREATED" } },
    });
    show(<Tasks query={{}} />);
    fireEvent.click((await screen.findAllByRole("button", { name: "Новая задача" }))[0] as HTMLElement);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect((within(dialog).getByLabelText("Проект") as HTMLSelectElement).value).toBe("p1"));
    fireEvent.click(within(dialog).getByRole("radio", { name: "develop" }));
    fireEvent.change(within(dialog).getByLabelText("Задача"), { target: { value: "Add tax" } });
    fireEvent.change(within(dialog).getByLabelText("Описание и критерии приёмки"), { target: { value: "VAT 12%" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Запустить" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("no AI provider");
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ kind: "develop", params: { title: "Add tax", body: "VAT 12%" } });

    fireEvent.click(within(dialog).getByRole("radio", { name: "audit" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Запустить" }));
    await waitFor(() => expect(window.location.hash).toBe("#/runs/r9"));
    expect(calls.filter((c) => c.method === "POST").at(-1)?.body).toEqual({ kind: "audit", params: {} });
  });

  it("reads maintenance issues as 'title: body' lines", () => {
    expect(parseIssues("Login fails: 500 on empty password\n\nSlow export")).toEqual([
      { id: "I-1", title: "Login fails", body: "500 on empty password" },
      { id: "I-2", title: "Slow export", body: "" },
    ]);
  });
});

const DETAIL: RunDetail = {
  id: "r1",
  state: "RUNNING",
  mode: "audit",
  goal: "audit: the project",
  error: null,
  kind: "audit",
  project_id: "p1",
  tasks: [
    { key: "discover.config", phase: "discovery", status: "SUCCEEDED", attempts: 1, error: null },
    { key: "scan.semgrep", phase: "execution", status: "FAILED", attempts: 2, error: "image missing" },
  ],
  approvals: [],
  jobs: [{ kind: "execute", state: "leased", attempts: 1, error: null }],
  dependencies: [{ task: "scan.semgrep", depends_on: "discover.config" }],
  gates: [{ gate: "security", result: "FAIL", blocking: true, reason: "2 high findings" }],
  usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 },
};

describe("run page", () => {
  it("shows gates, steps, the graph and findings, and cancels", async () => {
    const calls = serve({
      "GET /runs/r1": DETAIL,
      "GET /projects/p1": { ...PROJECTS[0], members: [], kinds: [] },
      "GET /runs?limit=100&project_id=p1": [{ ...RUNS[0], id: "r1", state: "RUNNING" }],
      "GET /findings?limit=1000&run_id=r1": [],
      "GET /runs/r1/artifacts": [{ id: "x1", name: "audit-report.md", sha256: "s", media_type: "text/markdown", size_bytes: 1200 }],
      "POST /runs/r1/cancel": { state: "CANCELLED" },
    });
    show(<RunPage id="r1" />);
    expect(await screen.findByText("2 high findings")).toBeTruthy();
    expect(screen.getByText("security")).toBeTruthy();
    expect(await screen.findByText("alpha")).toBeTruthy(); // the project link
    fireEvent.click(screen.getByRole("tab", { name: /Шаги/ }));
    expect(screen.getByText("scan.semgrep")).toBeTruthy();
    expect(screen.getByText("image missing")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Граф/ }));
    expect(await screen.findByText(/graph: 2 steps, 1 links/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Находки/ }));
    expect(await screen.findByText("Находок нет")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Артефакты/ }));
    expect(await screen.findByText("audit-report.md")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/runs/r1/cancel")).toBe(true));
  });

  it("asks for a decision on pending approvals", async () => {
    const calls = serve({
      "GET /runs/r1": {
        ...DETAIL,
        state: "AWAITING_APPROVAL",
        approvals: [{ id: "a1", operation: "migration", status: "pending", reason: "schema change", decided_by: null }],
      },
      "GET /projects/p1": { ...PROJECTS[0], members: [], kinds: [] },
      "GET /runs?limit=100&project_id=p1": [],
      "POST /runs/r1/approve": { state: "RUNNING" },
    });
    show(<RunPage id="r1" />);
    expect(await screen.findByText("migration")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ approve: false, note: "" }));
  });
});

const exec = (agent: string, status: string, runId: string, finished: string | null, extra: Partial<AgentExecution> = {}): AgentExecution => ({
  id: `${agent}-${runId}`,
  run_id: runId,
  task_key: `${agent}.step`,
  agent_name: agent,
  attempt: 1,
  status,
  confidence: null,
  error: null,
  started_at: "2026-11-06T10:00:00Z",
  finished_at: finished,
  duration_seconds: finished ? 12 : null,
  input_tokens: 1000,
  output_tokens: 200,
  cost_usd: 0.01,
  models: ["anthropic/claude-x"],
  ...extra,
});
const agent = (name: string) => ({
  name,
  inputs: "Issue, контекст",
  outputs: `${name} output`,
  requires_llm: true,
  implemented: true,
  sprint: "S6",
  capabilities: [],
  allowed_tools: [],
});

describe("agents", () => {
  it("derives what each agent does from its executions", async () => {
    useUi.setState({ projectId: "p1" });
    serve({
      "GET /projects/p1": { ...PROJECTS[0], members: [], kinds: [] },
      "GET /agents": [agent("developer"), agent("reviewer"), agent("security")],
      "GET /projects/p1/agent-executions?limit=100": [
        exec("agent.developer", "running", "r1", null),
        exec("agent.reviewer", "failed", "r0", "2026-11-06T10:01:00Z", { error: "invalid diff" }),
        exec("builtin.audit.snapshot", "succeeded", "r1", "2026-11-06T10:00:05Z"),
      ],
      "GET /runs?limit=100&project_id=p1": [run("r1", "RUNNING", "develop", "p1", "develop: Add tax"), run("r0", "FAILED", "develop", "p1", "older")],
      "GET /runs/r1": { ...DETAIL, tasks: [{ key: "agent.developer.step", phase: "execution", status: "RUNNING", attempts: 1, error: null }], dependencies: [] },
    });
    show(<Agents />);
    const developer = await screen.findByRole("article", { name: "Агент developer" });
    expect(within(developer).getByText("В работе")).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Агент reviewer" })).getByText("Ошибка")).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Агент reviewer" })).getByText("invalid diff")).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Агент security" })).getByText("Свободен")).toBeTruthy();
    expect(screen.getByText("builtin.audit.snapshot")).toBeTruthy();

    fireEvent.click(screen.getByRole("radio", { name: /Поток работы/ }));
    expect(await screen.findByText(/graph: 1 steps, 0 links/)).toBeTruthy();
    expect(screen.getByText("agent.developer.step=developer")).toBeTruthy();
  });

  it("asks for a project first", () => {
    serve({ "GET /runs?limit=100": [] });
    show(<Agents />);
    expect(screen.getByText("Выберите проект")).toBeTruthy();
  });

  it("is waiting while its run waits for a person", () => {
    const list = [exec("agent.maintenance", "running", "r5", null)];
    expect(agentStatus(list, () => "AWAITING_APPROVAL")).toBe("waiting");
    expect(agentStatus(list, () => "RUNNING")).toBe("working");
    expect(agentStatus([], () => undefined)).toBe("idle");
  });
});
