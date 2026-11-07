import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "../data/queries";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
import Infrastructure, { PULL_COMMAND } from "./Infrastructure";

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

beforeEach(() => {
  queryClient.clear();
  useUi.setState({ projectId: null });
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
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

  it("counts workers online by their heartbeat and says what each does", async () => {
    const system = {
      ...SYSTEM,
      workers: [
        { id: "h:1:a", online: true, active_jobs: 0, last_seen: "2026-11-06T10:00:00Z", host: "h", started: "2026-11-06T09:00:00Z", concurrency: 10 },
        { id: "h:2:b", online: false, active_jobs: 0, last_seen: "2026-11-06T08:00:00Z", host: "h", started: "2026-11-06T07:00:00Z", concurrency: 10 },
      ],
    };
    serve((path) => (path === "/system" ? system : path === "/readyz" ? { status: "ok" } : undefined));
    show(<Infrastructure me={{ username: "root", is_admin: true }} />);
    expect(await screen.findByText("1 в сети")).toBeTruthy();
    expect(screen.getByText("ждёт заданий")).toBeTruthy();
    expect(screen.getByText("нет сигнала")).toBeTruthy();
  });

  it("shows developers' machines reporting local runs, or how to connect one", async () => {
    const overview = { machines: [], reports_24h: 0, projects: 0, last_report: null };
    serve((path) => (path === "/readyz" ? { status: "ok" } : path === "/local-runs/overview" ? overview : undefined));
    show(<Infrastructure me={{ username: "alice", is_admin: false }} />);
    expect(await screen.findByText("не подключено")).toBeTruthy();
    expect(screen.getByText(/ai-coding server login --url/)).toBeTruthy();
    cleanup();
    queryClient.clear();
    const machine = { machine: "laptop", user: "alice", reports: 3, project_id: "p1", project: "shop", mode: "audit", state: "COMPLETED", verdict: "PASS", cli_version: "2.1.0", reported_at: "2026-11-06T10:00:00Z" };
    serve((path) => (path === "/readyz" ? { status: "ok" } : path === "/local-runs/overview" ? { ...overview, machines: [machine], reports_24h: 3 } : undefined));
    show(<Infrastructure me={{ username: "alice", is_admin: false }} />);
    expect(await screen.findByText("1 подключено")).toBeTruthy();
    expect(screen.getByText(/laptop · alice — shop/)).toBeTruthy();
    expect(screen.getByText("3 за сутки")).toBeTruthy();
  });

  it("shows a member the server's health only", async () => {
    const calls = serve((path) => (path === "/readyz" ? { status: "ok" } : undefined));
    show(<Infrastructure me={{ username: "alice", is_admin: false }} />);
    expect(await screen.findByText(/видят администраторы сервера/)).toBeTruthy();
    expect(calls).not.toContain("/system");
    expect(screen.queryByText(PULL_COMMAND)).toBeNull();
  });

  it("shows GitHub Actions of the member's projects, not a placeholder", async () => {
    const status = {
      connected: 1,
      enabled: 0,
      failing: 0,
      projects: [{ project_id: "p-1", name: "shop", repository: "acme/shop", enabled: false, token_set: true, connected: true, last: null, can_manage: true }],
    };
    serve((path) => (path === "/readyz" ? { status: "ok" } : path === "/github/status" ? status : undefined));
    show(<Infrastructure me={{ username: "alice", is_admin: false }} />);
    expect(await screen.findByText(/Подключено проектов: 1 из 1, включено: 0/)).toBeTruthy();
    expect(await screen.findByText("GitHub Actions выключены")).toBeTruthy();
    for (const link of screen.getAllByRole("link", { name: /Настроить/ })) {
      expect(link.getAttribute("href")).toBe("#/projects/p-1?tab=integrations"); // the row and the empty state
    }
    expect(screen.queryByText(/сервер их не опрашивает/)).toBeNull();
  });
});
