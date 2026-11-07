import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentExecution, RunDetail } from "../api";
import { queryClient } from "../data/queries";
import { TooltipProvider } from "../ui";
import AgentWorkspace, { agentsOfHandler, workParams } from "./AgentWorkspace";

// The agents' workspace against a fake API: routes "METHOD path" → a JSON body, or a raw
// text for artifacts (a diff is not JSON).
type Route = unknown | { text: string };
function serve(routes: Record<string, Route>) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace("/api/v1", "");
      const method = init.method ?? "GET";
      calls.push({ method, path, body: typeof init.body === "string" ? JSON.parse(init.body) : undefined });
      const key = `${method} ${path}`;
      if (!(key in routes)) return new Response(JSON.stringify({ error: { message: `no route ${key}` } }), { status: 404 });
      const route = routes[key];
      if (route && typeof route === "object" && "text" in route) return new Response((route as { text: string }).text, { status: 200 });
      return new Response(JSON.stringify(route), { status: 200 });
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

const READY = {
  effective: { enabled: true, provider: "openai", model: "gpt-5.5", private: false },
  key: { set: true, source: "web", env: null },
  valid: true,
  error: null,
};

function detail(over: Partial<RunDetail>): RunDetail {
  return { id: "r1", state: "RUNNING", mode: "develop", goal: "develop: Экспорт отчёта в CSV", error: null, kind: "develop", project_id: "p1", tasks: [], approvals: [], jobs: [], ...over };
}

function exec(handler: string, status: string, runId: string, over: Partial<AgentExecution> = {}): AgentExecution {
  return {
    id: `${handler}-${runId}`,
    run_id: runId,
    task_key: "develop.implement",
    agent_name: handler,
    attempt: 1,
    status,
    confidence: null,
    error: null,
    started_at: "2026-11-07T10:00:00Z",
    finished_at: status === "running" ? null : "2026-11-07T10:03:00Z",
    duration_seconds: status === "running" ? null : 180,
    input_tokens: 12000,
    output_tokens: 3000,
    cost_usd: 0.08,
    models: ["openai/gpt-5.5"],
    ...over,
  };
}

beforeEach(() => {
  queryClient.clear();
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("agent workspace", () => {
  it("turns a task in plain words into the run's parameters", () => {
    expect(workParams("develop", "  Экспорт в CSV\nКнопка на странице отчёта  ", "", 3)).toEqual({
      title: "Экспорт в CSV",
      body: "Экспорт в CSV\nКнопка на странице отчёта",
      max_iterations: 3,
    });
    expect(workParams("develop", "Короткая задача", "ai/x", 2)).toEqual({ title: "Короткая задача", max_iterations: 2, base: "ai/x" });
    expect(workParams("maintain", "Округление: теряются копейки\nОбнови requests", "", 3)).toEqual({
      issues: [
        { id: "I-1", title: "Округление", body: "теряются копейки" },
        { id: "I-2", title: "Обнови requests", body: "" },
      ],
    });
    expect(agentsOfHandler("builtin.develop.implement")).toEqual(["developer", "reviewer"]);
    expect(agentsOfHandler("agent.security.triage")).toEqual(["security"]);
    expect(agentsOfHandler("builtin.audit.snapshot")).toEqual([]);
  });

  it("gives the agents a task and follows the run: steps, the agents at work, an approval", async () => {
    const calls = serve({
      "GET /projects/p1/ai": READY,
      "GET /runs?limit=100&project_id=p1": [],
      "POST /projects/p1/runs": { id: "r1" },
      "GET /runs/r1": detail({
        tasks: [
          { key: "discover.git", phase: "discovery", status: "SUCCEEDED", attempts: 1, error: null },
          { key: "develop.implement", phase: "execution", status: "WAITING_APPROVAL", attempts: 1, error: null },
          { key: "develop.verify", phase: "verification", status: "PENDING", attempts: 0, error: null },
        ],
        approvals: [{ id: "a1", operation: "external_llm", status: "pending", reason: "sending context to external provider 'openai'", decided_by: null }],
      }),
      "GET /projects/p1/agent-executions?limit=500": [exec("builtin.develop.implement", "needs_approval", "r1"), exec("builtin.develop.implement", "succeeded", "other")],
      "GET /runs/r1/artifacts": [],
      "POST /runs/r1/approve": { state: "RUNNING" },
    });
    show(<AgentWorkspace projectId="p1" />);
    expect(await screen.findByText(/Модель агентов:/)).toBeTruthy();
    expect(screen.getByText("Дайте агентам задачу")).toBeTruthy();
    const start = screen.getByRole("button", { name: /Запустить агентов/ }) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Что сделать"), { target: { value: "Экспорт отчёта в CSV\nКнопка «Скачать CSV», тесты на формат" } });
    fireEvent.click(start);
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeTruthy());
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      kind: "develop",
      params: { title: "Экспорт отчёта в CSV", body: "Экспорт отчёта в CSV\nКнопка «Скачать CSV», тесты на формат", max_iterations: 3 },
    });

    // The new run opens: what the agents wait for comes first.
    const approval = await screen.findByRole("group", { name: "Агенты ждут вашего решения" });
    expect(within(approval).getByText(/Отправить код и контекст проекта внешней модели/)).toBeTruthy();
    const steps = screen.getByRole("list", { name: "Шаги" });
    expect(within(steps).getByText("Реализация: код, тесты, ревью")).toBeTruthy();
    expect(within(steps).getByText(/Developer ↔ Reviewer/)).toBeTruthy();
    expect(within(steps).getByText("ждёт согласия")).toBeTruthy();
    const agents = screen.getByRole("list", { name: "Агенты в этой задаче" });
    expect(within(agents).getAllByRole("listitem")).toHaveLength(1); // only this run's
    expect(within(agents).getByText("Developer ↔ Reviewer")).toBeTruthy();

    fireEvent.change(within(approval).getByLabelText("Комментарий к решению"), { target: { value: "ok" } });
    fireEvent.click(within(approval).getByRole("button", { name: "Разрешить" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/runs/r1/approve")?.body).toEqual({ approve: true, note: "ok" }));
  });

  it("shows what the agents produced and continues from the result branch", async () => {
    const calls = serve({
      "GET /projects/p1/ai": READY,
      "GET /runs?limit=100&project_id=p1": [
        { id: "r2", project_id: "p1", project_name: "alpha", state: "COMPLETED", mode: "develop", kind: "develop", goal: "develop: Экспорт отчёта в CSV", requested_by: "dev", created_at: "2026-11-07T09:00:00Z", finished_at: "2026-11-07T09:20:00Z" },
        { id: "r0", project_id: "p1", project_name: "alpha", state: "COMPLETED", mode: "audit", kind: "audit", goal: "audit", requested_by: "dev", created_at: "2026-11-07T08:00:00Z", finished_at: null },
      ],
      "GET /runs/r2": detail({
        id: "r2",
        state: "COMPLETED",
        tasks: [{ key: "develop.implement", phase: "execution", status: "SUCCEEDED", attempts: 1, error: null }],
        results: {
          "develop.implement": { branch: "ai/export-csv", commit: "abcdef1234567890", iterations: 2, summary: "Добавлен экспорт" },
          "github.pr": { url: "https://github.com/o/r/pull/7", number: 7 },
        },
      }),
      "GET /projects/p1/agent-executions?limit=500": [],
      "GET /runs/r2/artifacts": [
        { id: "impl", name: "implementation.json", sha256: "x", media_type: "application/json", size_bytes: 10 },
        { id: "diff", name: "change.diff", sha256: "y", media_type: "text/x-diff", size_bytes: 10 },
      ],
      "GET /runs/r2/artifacts/impl": {
        text: JSON.stringify({
          summary: "Добавлен экспорт отчёта в CSV с тестами",
          plan: ["Сериализатор CSV", "Кнопка на странице", "Тесты формата"],
          changes: [{ path: "app/export.py", op: "create" }, { path: "tests/test_export.py", op: "create" }],
          review: { verdict: "approve", findings: [] },
          iterations: 2,
        }),
      },
      "GET /runs/r2/artifacts/diff": { text: "--- a/app/export.py\n+++ b/app/export.py\n@@ -0,0 +1 @@\n+def export(rows): ..." },
      "POST /projects/p1/runs": { id: "r3" },
      "GET /runs/r3": detail({ id: "r3" }),
      "GET /runs/r3/artifacts": [],
    });
    show(<AgentWorkspace projectId="p1" initialRun="r2" />);
    const list = await screen.findByRole("list", { name: "Задачи агентов" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1); // the agents' tasks, not audits
    expect(within(list).getByRole("button").getAttribute("aria-current")).toBe("true");
    expect(await screen.findByText("Добавлен экспорт отчёта в CSV с тестами")).toBeTruthy();
    expect(screen.getByText("ai/export-csv")).toBeTruthy();
    expect(screen.getByText("abcdef1234")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Pull request #7/ }).getAttribute("href")).toBe("https://github.com/o/r/pull/7");
    expect(screen.getByText("кругов Developer ↔ Reviewer: 2")).toBeTruthy();
    expect(screen.getByText("ревью: approve")).toBeTruthy();
    expect(screen.getByText("Тесты формата")).toBeTruthy(); // the plan
    expect(screen.getByText("app/export.py", { selector: "code" })).toBeTruthy();
    expect((await screen.findByText(/def export/)).className).toBe("add");

    fireEvent.change(screen.getByLabelText("Доработать результат"), { target: { value: "Добавь заголовок с датой отчёта" } });
    fireEvent.click(screen.getByRole("button", { name: "Доработать" }));
    const box = screen.getByLabelText("Что сделать") as HTMLTextAreaElement;
    expect(box.value).toMatch(/^Доработка: Добавь заголовок с датой отчёта\nПродолжи задачу «develop: Экспорт отчёта в CSV» в ветке ai\/export-csv\./);
    expect((screen.getByLabelText("Базовая ветка") as HTMLInputElement).value).toBe("ai/export-csv");
    fireEvent.click(screen.getByRole("button", { name: /Запустить агентов/ }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeTruthy());
    const params = (calls.find((c) => c.method === "POST")?.body as { params: Record<string, unknown> }).params;
    expect(params.title).toBe("Доработка: Добавь заголовок с датой отчёта");
    expect(params.base).toBe("ai/export-csv");
  });

  it("maintenance: every line is an issue for the Maintenance agent", async () => {
    const calls = serve({
      "GET /projects/p1/ai": READY,
      "GET /runs?limit=100&project_id=p1": [],
      "POST /projects/p1/runs": { id: "r4" },
      "GET /runs/r4": detail({ id: "r4", kind: "maintain", goal: "maintain: 2 issues", tasks: [{ key: "maintain.plan", phase: "planning", status: "RUNNING", attempts: 1, error: null }] }),
      "GET /projects/p1/agent-executions?limit=500": [exec("agent.maintenance", "running", "r4", { task_key: "maintain.plan" })],
      "GET /runs/r4/artifacts": [],
    });
    show(<AgentWorkspace projectId="p1" />);
    fireEvent.click(await screen.findByRole("radio", { name: "Исправления" }));
    expect(screen.queryByLabelText("Кругов Developer ↔ Reviewer")).toBeNull();
    fireEvent.change(screen.getByLabelText("Что сделать"), { target: { value: "Округление: теряются копейки\nОбнови requests" } });
    fireEvent.click(screen.getByRole("button", { name: /Запустить агентов/ }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeTruthy());
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      kind: "maintain",
      params: { issues: [{ id: "I-1", title: "Округление", body: "теряются копейки" }, { id: "I-2", title: "Обнови requests", body: "" }] },
    });
    expect(await screen.findByText("План исправлений")).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "Агенты в этой задаче" })).getByText("Maintenance")).toBeTruthy();
  });

  it("says what is missing before the agents can work, and retries a failed task", async () => {
    serve({
      "GET /projects/p1/ai": { ...READY, effective: { enabled: false, provider: "none", model: null, private: false }, key: { set: false } },
      "GET /runs?limit=100&project_id=p1": [],
      "GET /runs/r5": detail({ id: "r5", state: "FAILED", error: "Developer: the provider refused the request", goal: "develop: Экспорт отчёта" }),
      "GET /projects/p1/agent-executions?limit=500": [],
      "GET /runs/r5/artifacts": [],
    });
    show(<AgentWorkspace projectId="p1" initialRun="r5" />);
    expect(await screen.findByText(/ИИ-провайдер проекта не включён/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Настроить модели" }).getAttribute("href")).toBe("#/models");
    expect(await screen.findByText("Developer: the provider refused the request")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Уточнить и запустить заново/ }));
    expect((screen.getByLabelText("Что сделать") as HTMLTextAreaElement).value).toBe("Экспорт отчёта\n");
  });
});
