import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Deployment, Environment } from "../data/deploy";
import { queryClient } from "../data/queries";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
import Deployments from "./Deployments";

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
      return new Response(JSON.stringify(body), { status: call.method === "POST" ? 201 : 200 });
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
const DONE: Deployment = {
  id: "d-1",
  environment_id: "e-staging",
  environment: "staging",
  ref: "main",
  commit: "4d8b12676a8af94836549d929ec914b662a43d9c",
  state: "succeeded",
  external_url: "https://cd.example.com/d/42",
  detail: null,
  requested_by: "maint",
  run_id: "r-1",
  created_at: "2026-11-06T10:00:00Z",
  finished_at: "2026-11-06T10:02:00Z",
};

function env(over: Partial<Environment>): Environment {
  return {
    id: "e-staging",
    name: "staging",
    kind: "webhook",
    production: false,
    requires_approval: false,
    require_gates: false,
    position: 0,
    workflow: null,
    environment_input: null,
    ref_input: null,
    webhook_url: "https://deploy.example.com/hooks/ai",
    health_url: null,
    timeout_minutes: 30,
    secret_name: "deploy.staging.webhook",
    created_at: null,
    updated_at: null,
    last_deployment: null,
    ...over,
  };
}

const ENVS = [env({ last_deployment: DONE }), env({ id: "e-prod", name: "production", production: true, requires_approval: true, secret_name: "deploy.production.webhook" })];

function backend(role: { manage: boolean; deploy: boolean }, extra?: Handler) {
  return serve((call) => {
    const answer = extra?.(call);
    if (answer !== undefined) return answer;
    if (call.path === "/projects") return [{ id: P, name: "shop", role: role.manage ? "maintainer" : "developer", created_at: "2026-11-01T00:00:00Z" }];
    if (call.path === `/projects/${P}/environments` && call.method === "GET") return { environments: ENVS, can_manage: role.manage, can_deploy: role.deploy };
    if (call.path.startsWith(`/projects/${P}/deployments`) && call.method === "GET") return [DONE];
    if (call.path === `/projects/${P}/github`)
      return { data: { enabled: true, repository: "acme/shop", default_ref: "release" }, version: 1, updated_by: "o", updated_at: null, enabled: true, token_set: true, token_hint: null, can_manage: false, last: null };
    if (call.path === `/projects/${P}/secrets`) return { available: true, secrets: [] };
    return undefined;
  });
}

beforeEach(() => {
  queryClient.clear();
  useUi.setState({ projectId: P });
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("deployments", () => {
  it("shows the environments with their latest deployment and the history", async () => {
    backend({ manage: true, deploy: true });
    show(<Deployments />);
    const staging = await screen.findByRole("region", { name: "Среда staging" });
    expect(within(staging).getByText("успешно")).toBeTruthy();
    expect(within(staging).getByText("4d8b126")).toBeTruthy();
    expect(within(staging).getByLabelText("Деплой во внешней системе").getAttribute("href")).toBe("https://cd.example.com/d/42");
    const prod = screen.getByRole("region", { name: "Среда production" });
    expect(within(prod).getAllByText("production")).toHaveLength(2); // the name and the badge
    expect(within(prod).getByText("с подтверждением")).toBeTruthy();
    expect(within(prod).getByText("Деплоев ещё не было.")).toBeTruthy();
    const history = await screen.findByRole("region", { name: "История деплоев" });
    expect(within(history).getByText("maint")).toBeTruthy();
    expect(within(history).getByText("staging")).toBeTruthy();
  });

  it("asks for the name of a production environment and says it waits for approval", async () => {
    const calls = backend({ manage: true, deploy: true }, (call) =>
      call.method === "POST" && call.path === `/projects/${P}/deployments` ? { ...DONE, id: "d-2", state: "queued", run_id: "r-2" } : undefined,
    );
    show(<Deployments />);
    const prod = await screen.findByRole("region", { name: "Среда production" });
    fireEvent.click(within(prod).getByRole("button", { name: "Задеплоить" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/дождётся подтверждения/)).toBeTruthy();
    const ref = within(dialog).getByLabelText("Ветка, тег или коммит") as HTMLInputElement;
    await waitFor(() => expect(ref.value).toBe("release")); // the project's default ref
    const go = within(dialog).getByRole("button", { name: "Задеплоить" }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText("Имя среды для подтверждения"), { target: { value: "producti" } });
    expect(go.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText("Имя среды для подтверждения"), { target: { value: "production" } });
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ environment_id: "e-prod", ref: "release" });
    await waitFor(() => expect(window.location.hash).toBe("#/runs/r-2")); // to the approval
  });

  it("is read-only for a developer", async () => {
    backend({ manage: false, deploy: false });
    show(<Deployments />);
    await screen.findByRole("region", { name: "Среда staging" });
    expect(screen.queryByRole("button", { name: "Задеплоить" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Добавить среду" })).toBeNull();
    expect(screen.queryByLabelText("Изменить среду staging")).toBeNull();
    expect(screen.getByText("Деплоить могут владелец и мейнтейнеры проекта.")).toBeTruthy();
  });

  it("creates a webhook environment, then offers its signing secret", async () => {
    const created = env({ id: "e-new", name: "qa", secret_name: "deploy.qa.webhook", webhook_url: "https://qa.example.com/hook" });
    const calls = backend({ manage: true, deploy: true }, (call) => (call.method === "POST" && call.path === `/projects/${P}/environments` ? created : undefined));
    show(<Deployments />);
    fireEvent.click(await screen.findByRole("button", { name: "Добавить среду" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Имя"), { target: { value: "qa" } });
    fireEvent.change(within(dialog).getByLabelText(/Адрес webhook/), { target: { value: "https://qa.example.com/hook" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({
      name: "qa",
      kind: "webhook",
      webhook_url: "https://qa.example.com/hook",
      workflow: null,
      production: false,
    });
    expect(await within(await screen.findByRole("dialog")).findByText("Секрет подписи webhook")).toBeTruthy();
  });

  it("asks for a project when none is chosen", async () => {
    useUi.setState({ projectId: null });
    backend({ manage: true, deploy: true });
    show(<Deployments />);
    expect(await screen.findByText("Деплой настраивается для каждого проекта отдельно.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Проект"), { target: { value: P } });
    expect(await screen.findByRole("region", { name: "Среда staging" })).toBeTruthy();
  });
});
