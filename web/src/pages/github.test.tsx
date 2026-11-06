import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GitHubSettings, GitHubStatus } from "../data/github";
import { queryClient } from "../data/queries";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
import { GitHubActionsPanel, GitHubEnvCard, GitHubIntegration, integrationsHref } from "./GitHub";

type Call = { method: string; path: string; body: unknown };
type Handler = (call: Call) => unknown;
function serve(handler: Handler) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call = { method: init?.method ?? "GET", path: url.replace("/api/v1", ""), body: init?.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(call);
      const body = handler(call);
      if (body === undefined) return new Response(JSON.stringify({ error: { message: `no route ${call.path}` } }), { status: 404 });
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

const P = "p-1";
function settings(over: Partial<GitHubSettings> = {}): GitHubSettings {
  return {
    data: { enabled: false, repository: "acme/shop", api_url: "https://api.github.com", default_ref: "main" },
    version: 3,
    updated_by: "owner",
    updated_at: "2026-11-06T09:00:00Z",
    enabled: false,
    token_set: true,
    token_hint: "…cdef",
    can_manage: true,
    last: null,
    ...over,
  };
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

describe("the project's integrations tab", () => {
  it("turns GitHub Actions on and checks the connection", async () => {
    const calls = serve((call) => {
      if (call.path === `/projects/${P}/github` && call.method === "GET") return settings();
      if (call.path === `/projects/${P}/github` && call.method === "PUT") return { data: call.body, version: 4 };
      if (call.path === `/projects/${P}/github/check`)
        return { ok: true, checked_at: "2026-11-06T10:00:00Z", repository: "acme/shop", private: true, default_branch: "main", html_url: "https://github.com/acme/shop", permissions: { pull: true, push: true, admin: false } };
      if (call.path === `/projects/${P}/secrets`) return { available: true, secrets: [{ name: "github.token", hint: "…cdef", updated_by: "owner", updated_at: "2026-11-06T09:00:00Z" }] };
      return undefined;
    });
    show(<GitHubIntegration projectId={P} />);
    const toggle = (await screen.findByRole("switch", { name: "Включить GitHub Actions" })) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    expect(await screen.findByText(/задан …cdef/)).toBeTruthy(); // the token: its hint only
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.path === `/projects/${P}/github`)).toBe(true));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ enabled: true, repository: "acme/shop", default_ref: "main", version: 3 });

    fireEvent.click(screen.getByRole("button", { name: "Проверить подключение" }));
    const result = await screen.findByText("Подключение работает:");
    const box = result.closest("[role=status]") as HTMLElement;
    expect(within(box).getByText("acme/shop").getAttribute("href")).toBe("https://github.com/acme/shop");
    expect(within(box).getByText(/Права токена в репозитории: pull, push\./)).toBeTruthy();
  });

  it("explains a failed check in words", async () => {
    serve((call) => {
      if (call.path === `/projects/${P}/github`) return settings({ enabled: true });
      if (call.path === `/projects/${P}/github/check`)
        return { ok: false, checked_at: "2026-11-06T10:00:00Z", reason: "bad_token", error: "GitHub rejected the token (401): it is invalid, expired or revoked — set a new one" };
      if (call.path === `/projects/${P}/secrets`) return { available: true, secrets: [] };
      return undefined;
    });
    show(<GitHubIntegration projectId={P} />);
    fireEvent.click(await screen.findByRole("button", { name: "Проверить подключение" }));
    expect(await screen.findByText(/GitHub отклонил токен/)).toBeTruthy();
    expect(screen.getByText("Подключение не работает.")).toBeTruthy();
  });

  it("is read-only for those who do not manage the project", async () => {
    const calls = serve((call) => (call.path === `/projects/${P}/github` ? settings({ can_manage: false, token_hint: null, enabled: true }) : undefined));
    show(<GitHubIntegration projectId={P} />);
    const toggle = (await screen.findByRole("switch", { name: "Включить GitHub Actions" })) as HTMLInputElement;
    expect(toggle.disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Сохранить" })).toBeNull();
    expect(screen.getByText(/Токен: задан\./)).toBeTruthy();
    expect(calls.some((c) => c.path.endsWith("/secrets"))).toBe(false); // no secrets listing
  });
});

describe("GitHub Actions on the infrastructure page", () => {
  const STATUS: GitHubStatus = {
    connected: 2,
    enabled: 2,
    failing: 1,
    projects: [
      { project_id: P, name: "shop", repository: "acme/shop", enabled: true, token_set: true, connected: true, last: { ok: true, error: null, reason: null, kind: "poll", at: "2026-11-06T10:00:00Z" }, can_manage: true },
      {
        project_id: "p-2",
        name: "blog",
        repository: "acme/blog",
        enabled: true,
        token_set: true,
        connected: true,
        last: { ok: false, error: "the token may not do this (403)", reason: "forbidden", kind: "poll", at: "2026-11-06T10:00:00Z" },
        can_manage: false,
      },
      { project_id: "p-3", name: "docs", repository: null, enabled: false, token_set: false, connected: false, last: null, can_manage: true },
    ],
  };
  const RUN = {
    id: 501,
    name: "CI",
    title: "Fix the cart",
    workflow_id: 9,
    path: ".github/workflows/ci.yml",
    status: "completed",
    conclusion: "failure",
    branch: "main",
    sha: "a".repeat(40),
    event: "push",
    actor: "octo",
    run_number: 12,
    started_at: "2026-11-06T10:00:00Z",
    updated_at: "2026-11-06T10:03:20Z",
    duration_seconds: 200,
    html_url: "https://github.com/acme/shop/actions/runs/501",
  };

  function backend() {
    return serve((call) => {
      if (call.path === "/github/status") return STATUS;
      if (call.path === "/projects") return [{ id: P, name: "shop", role: "developer", created_at: "2026-11-01T00:00:00Z" }];
      if (call.path === `/projects/${P}/github`) return settings({ enabled: true, can_manage: false });
      if (call.path.startsWith(`/projects/${P}/github/runs?`)) return [RUN];
      if (call.path === `/projects/${P}/github/runs/501/jobs`)
        return [{ id: 1, name: "test", status: "completed", conclusion: "failure", started_at: null, completed_at: null, html_url: null, steps: [{ number: 1, name: "pytest", status: "completed", conclusion: "failure" }] }];
      if (call.path === `/projects/${P}/github/workflows`) return [{ id: 9, name: "Deploy", path: ".github/workflows/deploy.yml", file: "deploy.yml", state: "active", html_url: "" }];
      return undefined;
    });
  }

  it("sums up the projects in the environments grid", async () => {
    backend();
    show(<GitHubEnvCard />);
    expect(await screen.findByText("ошибок: 1")).toBeTruthy();
    expect(screen.getByText(/Подключено проектов: 2 из 3, включено: 2/)).toBeTruthy();
    expect(screen.queryByText(/не опрашивает/)).toBeNull();
  });

  it("lists every project's connection, with errors and where to set it up", async () => {
    useUi.setState({ projectId: P });
    backend();
    show(<GitHubActionsPanel />);
    const list = await screen.findByRole("list", { name: "Подключение проектов к GitHub" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(within(rows[1] as HTMLElement).getByText("ошибка")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText(/Токену не хватает прав/)).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByText("не подключён")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByText(/Настроить/).closest("a")?.getAttribute("href")).toBe(integrationsHref("p-3"));
    expect(integrationsHref("p-3")).toBe("#/projects/p-3?tab=integrations");

    // The current project's runs; its jobs on demand.
    const runs = await screen.findByRole("list", { name: "Запуски GitHub Actions" });
    expect(within(runs).getByText("Fix the cart")).toBeTruthy();
    expect(within(runs).getByText("провал")).toBeTruthy();
    fireEvent.click(within(runs).getByRole("button", { name: "Задания" }));
    expect(await screen.findByText("Шаг с ошибкой: pytest")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Запустить workflow" })).toBeTruthy(); // a developer may
  });

  it("dispatches a workflow with inputs", async () => {
    useUi.setState({ projectId: P });
    const calls = backend();
    const original = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) =>
        init?.method === "POST" ? new Response(JSON.stringify({ ok: true, workflow: "deploy.yml", ref: "main" }), { status: 200 }) : original(url, init),
      ),
    );
    show(<GitHubActionsPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Запустить workflow" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: "Deploy (deploy.yml)" });
    fireEvent.change(within(dialog).getByLabelText(/Входные параметры/), { target: { value: "environment=staging\n" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Запустить" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const post = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls.find(([, i]) => i?.method === "POST");
    expect(post?.[0]).toBe(`/api/v1/projects/${P}/github/workflows/deploy.yml/dispatch`);
    expect(JSON.parse(String(post?.[1].body))).toEqual({ ref: "main", inputs: { environment: "staging" } });
    expect(calls.length).toBeGreaterThan(0);
  });
});
