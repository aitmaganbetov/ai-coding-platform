import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FindingRow } from "../api";
import { queryClient } from "../data/queries";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
import AuditCenter, { categoryOf, compareAudits, fixDraft } from "./AuditCenter";
import Infrastructure, { PULL_COMMAND } from "./Infrastructure";
import Models from "./Models";
import Testing from "./Testing";

// Recharts measures its container; jsdom has no layout: give charts a fixed size.
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children) ? cloneElement(children as ReactElement<{ width: number; height: number }>, { width: 600, height: 220 }) : null,
  };
});

type Handler = (path: string) => unknown;
function serve(handler: Handler) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.replace("/api/v1", "");
      calls.push(path);
      const body = handler(path);
      if (body === undefined) return new Response(JSON.stringify({ error: { message: `no route ${path}` } }), { status: 404 });
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  return calls;
}

function show(ui: ReactNode) {
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  );
}

const PROJECTS = [{ id: "p1", name: "alpha", role: "owner", created_at: "2026-11-01T10:00:00Z" }];

function finding(over: Partial<FindingRow>): FindingRow {
  return {
    id: "f",
    project_id: "p1",
    project_name: "alpha",
    run_id: "r2",
    source: "semgrep",
    rule_id: null,
    severity: "high",
    title: "t",
    message: "",
    file_path: null,
    line: null,
    status: "open",
    fingerprint: "fp",
    evidence: {},
    created_at: "2026-11-05T10:00:00Z",
    ...over,
  };
}

const SHELL = finding({
  id: "f1",
  title: "subprocess with shell=True",
  rule_id: "python.lang.security.audit.subprocess-shell-true",
  file_path: "app/very/long/path/to/the/module/run.py",
  line: 4,
  fingerprint: "fp-shell",
  evidence: { snippet: "subprocess.call(cmd, shell=True)" },
  message: "Command injection risk",
});
const LINT = finding({ id: "f2", source: "ruff", severity: "low", title: "Line too long", file_path: "app/x.py", line: 1, fingerprint: "fp-lint" });

beforeEach(() => {
  queryClient.clear();
  useUi.setState({ projectId: null });
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("audit center", () => {
  it("categorises findings by the tool that found them", () => {
    expect(categoryOf("semgrep")).toBe("security");
    expect(categoryOf("gitleaks")).toBe("security");
    expect(categoryOf("osv-scanner")).toBe("dependencies");
    expect(categoryOf("trivy")).toBe("dependencies");
    expect(categoryOf("ruff")).toBe("quality");
    expect(categoryOf("eslint")).toBe("quality");
    expect(categoryOf("k6")).toBe("performance");
    expect(categoryOf("agent.database")).toBe("database");
    expect(categoryOf("architect")).toBe("architecture");
    expect(categoryOf("something-else")).toBe("other");
  });

  it("compares two audits by fingerprint", () => {
    const a = [finding({ fingerprint: "x" }), finding({ fingerprint: "y" })];
    const b = [finding({ fingerprint: "y" }), finding({ fingerprint: "z" })];
    const diff = compareAudits(a, b);
    expect(diff.added.map((f) => f.fingerprint)).toEqual(["z"]);
    expect(diff.fixed.map((f) => f.fingerprint)).toEqual(["x"]);
    expect(diff.kept.map((f) => f.fingerprint)).toEqual(["y"]);
    // Without a fingerprint the location identifies a finding.
    const loose = compareAudits([finding({ fingerprint: "", file_path: "a.py", line: 1 })], [finding({ fingerprint: "", file_path: "a.py", line: 1 })]);
    expect(loose.kept).toHaveLength(1);
  });

  it("filters by the severity in the address and hands a finding to the chat", async () => {
    serve((path) => {
      if (path === "/projects") return PROJECTS;
      if (path.startsWith("/findings")) return [SHELL, LINT];
      if (path.startsWith("/runs")) return [];
      return undefined;
    });
    show(<AuditCenter query={{ severity: "high" }} />);
    const row = await screen.findByText("subprocess with shell=True");
    expect(screen.queryByText("Line too long")).toBeNull();
    expect((screen.getByLabelText("Важность") as HTMLSelectElement).value).toBe("high");

    fireEvent.click(row);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("app/very/long/path/to/the/module/run.py:4")).toBeTruthy();
    expect(within(dialog).getByText(/subprocess\.call/)).toBeTruthy(); // the evidence
    const link = within(dialog).getByRole("link", { name: /Исправить в чате/ });
    const [path, search] = (link.getAttribute("href") ?? "").split("?");
    expect(path).toBe("#/projects/p1/ai");
    const params = new URLSearchParams(search);
    expect(params.get("mode")).toBe("maintain");
    expect(params.get("draft")).toBe(fixDraft(SHELL));
    expect(params.get("draft")).toContain("app/very/long/path/to/the/module/run.py:4");
    expect(params.get("draft")).toContain("subprocess-shell-true");
  });

  it("explains an empty category instead of showing nothing", async () => {
    serve((path) => (path === "/projects" ? PROJECTS : path.startsWith("/findings") ? [LINT] : path.startsWith("/runs") ? [] : undefined));
    show(<AuditCenter query={{ category: "database" }} />);
    expect(await screen.findByText(/агент Database/)).toBeTruthy();
  });
});

describe("testing", () => {
  it("shows the gate history of the project's audits", async () => {
    useUi.setState({ projectId: "p1" });
    const runs = [
      { id: "r2", project_id: "p1", project_name: "alpha", state: "COMPLETED", mode: "audit", kind: "audit", goal: "audit", requested_by: "a", created_at: "2026-11-05T10:00:00Z", finished_at: "2026-11-05T10:05:00Z" },
      { id: "r1", project_id: "p1", project_name: "alpha", state: "COMPLETED", mode: "audit", kind: "audit", goal: "audit", requested_by: "a", created_at: "2026-11-04T10:00:00Z", finished_at: "2026-11-04T10:05:00Z" },
    ];
    const detail = (id: string, security: string) => ({
      id,
      state: "COMPLETED",
      mode: "audit",
      goal: "audit",
      error: null,
      kind: "audit",
      project_id: "p1",
      tasks: [],
      approvals: [],
      jobs: [],
      gates: [
        { gate: "build", result: "PASS", blocking: true, reason: null },
        { gate: "security", result: security, blocking: true, reason: security === "FAIL" ? "2 high findings" : null, evidence: ["semgrep: app/run.py:4"] },
      ],
    });
    serve((path) => {
      if (path === "/projects") return PROJECTS;
      if (path.startsWith("/runs?")) return runs;
      if (path === "/runs/r2") return detail("r2", "FAIL");
      if (path === "/runs/r1") return detail("r1", "PASS");
      return undefined;
    });
    show(<Testing />);
    expect(await screen.findByText("2 high findings")).toBeTruthy();
    const history = screen.getByRole("region", { name: "История Quality Gates" });
    await waitFor(() => expect(within(history).getAllByText("FAIL")).toHaveLength(1));
    expect(within(history).getAllByText("PASS").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("semgrep: app/run.py:4")).toBeTruthy();
    expect(screen.getByText(/API пока не отдаёт/)).toBeTruthy();
  });
});

describe("models", () => {
  it("shows the provider and private mode, and never asks for a key", async () => {
    useUi.setState({ projectId: "p1" });
    serve((path) => {
      if (path === "/projects/p1")
        return {
          ...PROJECTS[0],
          members: [],
          kinds: [],
          config: {
            stacks: ["python"],
            mode: "develop",
            execution: { backend: "docker", network: "none" },
            ai: { enabled: true, provider: "ollama", model: "qwen2.5-coder" },
            security: { network: "local-only", egress_allowlist: [] },
            budgets: { max_tokens_per_run: 500000, max_cost_usd_per_run: 5, max_duration_minutes: 120 },
            quality_gates: {},
          },
        };
      if (path.startsWith("/projects/p1/usage"))
        return {
          totals: { calls: 3, input_tokens: 1200, output_tokens: 300, cost_usd: 0 },
          by_model: [{ provider: "ollama", model: "qwen2.5-coder", calls: 3, input_tokens: 1200, output_tokens: 300, cost_usd: 0 }],
          daily: [{ date: "2026-11-05", input_tokens: 1200, output_tokens: 300, cost_usd: 0 }],
        };
      if (path.startsWith("/projects/p1/agent-executions"))
        return [
          { id: "e1", run_id: "r1", task_key: "develop.implement", agent_name: "developer", attempt: 1, status: "SUCCEEDED", confidence: 0.8, error: null, started_at: "2026-11-05T10:00:00Z", finished_at: "2026-11-05T10:01:00Z", duration_seconds: 60, input_tokens: 1200, output_tokens: 300, cost_usd: 0, models: ["ollama/qwen2.5-coder"] },
        ];
      return undefined;
    });
    show(<Models />);
    expect(await screen.findByText("настроен в .ai-coding.yml")).toBeTruthy();
    expect(screen.getAllByText("запрещён политикой local-only")).toHaveLength(2); // the cloud providers
    expect(screen.getByText(/Private Mode: политика проекта/)).toBeTruthy();
    expect(await screen.findByText("developer")).toBeTruthy();
    expect(screen.getAllByText("ollama/qwen2.5-coder").length).toBeGreaterThan(0);
    expect(document.querySelectorAll('input[type="password"], textarea').length).toBe(0);
    expect(screen.queryByLabelText(/ключ/i)).toBeNull();
  });

  it("asks for a project first", () => {
    serve(() => undefined);
    show(<Models />);
    expect(screen.getByText("Выберите проект")).toBeTruthy();
  });
});

describe("infrastructure", () => {
  const SYSTEM = {
    version: "2.1.0",
    database: "postgresql",
    workspaces: "on",
    queue: { queued: 1 },
    runs: { COMPLETED: 3 },
    workers: [],
    analysers: { trivy: false, "osv-scanner": true },
    time: "2026-11-06T10:00:00Z",
  };

  it("tells an administrator how to prepare the analysers", async () => {
    serve((path) => (path === "/system" ? SYSTEM : path === "/readyz" ? { status: "ok" } : undefined));
    show(<Infrastructure me={{ username: "root", is_admin: true }} />);
    expect(await screen.findByText(PULL_COMMAND)).toBeTruthy();
    expect(screen.getByText("Нет активных worker'ов")).toBeTruthy();
    expect(screen.getByText("trivy: нет офлайн-базы")).toBeTruthy();
    expect(await screen.findByText("API и база данных доступны")).toBeTruthy();
  });

  it("shows a member the server's health only", async () => {
    const calls = serve((path) => (path === "/readyz" ? { status: "ok" } : undefined));
    show(<Infrastructure me={{ username: "alice", is_admin: false }} />);
    expect(await screen.findByText(/видят администраторы сервера/)).toBeTruthy();
    expect(calls).not.toContain("/system");
    expect(screen.queryByText(PULL_COMMAND)).toBeNull();
  });
});
