import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AIState } from "../data/ai";
import { queryClient } from "../data/queries";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
import Models from "./Models";

// Recharts measures its container; jsdom has no layout: give charts a fixed size.
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children) ? cloneElement(children as ReactElement<{ width: number; height: number }>, { width: 600, height: 220 }) : null,
  };
});

const KEY = "sk-typed-IN-THE-BROWSER-0123456789";

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

const VIEW = {
  enabled: true,
  provider: "openai" as const,
  model: "gpt-a",
  base_url: null,
  routing: {},
  fallback: [],
  budgets: { max_tokens_per_run: 500000, max_cost_usd_per_run: 5, max_duration_minutes: 120 },
  private: false,
  network: "allowlist",
};

function aiState(over: Partial<AIState> = {}): AIState {
  return {
    settings: {},
    version: 0,
    updated_by: null,
    updated_at: null,
    file: VIEW,
    effective: VIEW,
    sources: { enabled: "file", provider: "file", model: "file", base_url: "file", routing: "file", fallback: "file", budgets: "file", private: "file" },
    key: { set: true, source: "web", env: null },
    valid: true,
    error: null,
    can_edit: true,
    secrets_available: true,
    agents: ["developer", "reviewer"],
    ...over,
  };
}

function common(call: Call): unknown {
  if (call.path === "/projects/p1") return { id: "p1", name: "alpha", role: "owner", created_at: "2026-11-01T10:00:00Z", members: [], kinds: [], config: null };
  if (call.path.startsWith("/projects/p1/usage"))
    return {
      totals: { calls: 3, input_tokens: 1200, output_tokens: 300, cost_usd: 0 },
      by_model: [{ provider: "ollama", model: "qwen2.5-coder", calls: 3, input_tokens: 1200, output_tokens: 300, cost_usd: 0 }],
      daily: [{ date: "2026-11-05", input_tokens: 1200, output_tokens: 300, cost_usd: 0 }],
    };
  if (call.path.startsWith("/projects/p1/agent-executions"))
    return [
      { id: "e1", run_id: "r1", task_key: "develop.implement", agent_name: "developer", attempt: 1, status: "SUCCEEDED", confidence: 0.8, error: null, started_at: "2026-11-05T10:00:00Z", finished_at: "2026-11-05T10:01:00Z", duration_seconds: 60, input_tokens: 1200, output_tokens: 300, cost_usd: 0, models: ["ollama/qwen2.5-coder"] },
    ];
  return undefined;
}

beforeEach(() => {
  queryClient.clear();
  useUi.setState({ projectId: null });
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("models", () => {
  it("an owner checks the connection, picks a model and saves the key write-only", async () => {
    useUi.setState({ projectId: "p1" });
    let secrets = [{ name: "ai.api_key", hint: "…cdef", updated_by: "owner", updated_at: "2026-11-05T10:00:00Z" }];
    const calls = serve((call) => {
      if (call.path === "/projects/p1/ai" && call.method === "GET") return aiState();
      if (call.path === "/projects/p1/ai" && call.method === "PUT")
        return aiState({ version: 1, settings: { model: "gpt-b" }, effective: { ...VIEW, model: "gpt-b" }, sources: { ...aiState().sources, model: "web" }, updated_by: "owner", updated_at: "2026-11-06T10:00:00Z" });
      if (call.path === "/projects/p1/ai/models") return { provider: "openai", models: ["gpt-a", "gpt-b"], reason: null };
      if (call.path === "/projects/p1/ai/test") return { ok: true, provider: "openai", model: "gpt-a", latency_ms: 42, models_offered: 2, detail: "" };
      if (call.path === "/projects/p1/secrets") return { available: true, secrets };
      if (call.path === "/projects/p1/secrets/ai.api_key" && call.method === "PUT") {
        secrets = secrets.map((s) => ({ ...s, hint: "…6789" }));
        return secrets[0];
      }
      return common(call);
    });
    show(<Models />);
    expect(await screen.findByText("Действует сейчас")).toBeTruthy();
    expect(screen.getAllByText("из .ai-coding.yml").length).toBeGreaterThan(3);
    expect(screen.getByRole("radio", { name: /^OpenAI/ }).getAttribute("aria-checked")).toBe("true");

    // The connection test, with the saved settings.
    fireEvent.click(screen.getByRole("button", { name: "Проверить подключение" }));
    expect(await screen.findByText("Подключение работает")).toBeTruthy();
    expect(screen.getByText(/42 мс · моделей у провайдера: 2/)).toBeTruthy();

    // The model comes from the provider's list.
    const select = (await screen.findByRole("option", { name: "gpt-b" })).closest("select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "gpt-b" } });
    expect(screen.getByText("задано в веб")).toBeTruthy();
    expect(screen.getByText("Есть несохранённые изменения")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Проверить подключение" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Сохранить настройки" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.path === "/projects/p1/ai")).toBe(true));
    expect(calls.find((c) => c.method === "PUT" && c.path === "/projects/p1/ai")?.body).toEqual({ settings: { model: "gpt-b" }, version: 0 });
    expect(await screen.findByText(/owner, /)).toBeTruthy(); // «Изменено в веб»

    // The key: a SecretField — stored keys show only their last characters.
    expect(await screen.findByText(/задан …cdef/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Заменить" }));
    const input = screen.getByLabelText("API-ключ OpenAI") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: KEY } });
    fireEvent.click(within(input.closest("form") as HTMLElement).getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText(/задан …6789/)).toBeTruthy();
    expect(calls.find((c) => c.path === "/projects/p1/secrets/ai.api_key")?.body).toEqual({ value: KEY });
    // The key went to the secret store only, and the page does not keep it.
    expect(calls.filter((c) => c.path !== "/projects/p1/secrets/ai.api_key").some((c) => JSON.stringify(c.body ?? "").includes(KEY))).toBe(false);
    expect(document.body.innerHTML).not.toContain(KEY);
    // Kept: the usage and the agents' models.
    expect(await screen.findByText("developer", { selector: ".cell-main" })).toBeTruthy();
    expect(screen.getByText(/по вашей собственной подписке/)).toBeTruthy();
  });

  it("is read-only for a developer: no key field, no saving, no provider calls", async () => {
    useUi.setState({ projectId: "p1" });
    const calls = serve((call) => {
      if (call.path === "/projects/p1/ai") return aiState({ can_edit: false, key: { set: false, source: "env", env: "OPENAI_API_KEY" } });
      return common(call);
    });
    show(<Models />);
    expect(await screen.findByText(/Только просмотр/)).toBeTruthy();
    for (const el of screen.getAllByRole("switch")) expect((el as HTMLInputElement).disabled).toBe(true);
    for (const el of within(screen.getByRole("radiogroup", { name: "Провайдер" })).getAllByRole("radio")) expect((el as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("textbox", { name: "Модель" }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Проверить подключение" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: /Сохранить/ })).toBeNull();
    expect(document.querySelectorAll('input[type="password"]').length).toBe(0);
    expect(screen.getAllByText("OPENAI_API_KEY").length).toBeGreaterThan(0); // a variable's name, not a key
    expect(calls.some((c) => c.path.startsWith("/projects/p1/ai/") || c.path.startsWith("/projects/p1/secrets"))).toBe(false);
  });

  it("Private Mode keeps the cloud providers off; a failed check shows why", async () => {
    useUi.setState({ projectId: "p1" });
    const local = { ...VIEW, provider: "ollama" as const, model: "qwen2.5-coder", base_url: "http://localhost:11434/v1", private: true, network: "local-only" };
    serve((call) => {
      if (call.path === "/projects/p1/ai" && call.method === "GET") return aiState({ file: local, effective: local, key: { set: false, source: null, env: null } });
      if (call.path === "/projects/p1/ai/models") return { provider: "ollama", models: [], reason: "the provider lists no models" };
      if (call.path === "/projects/p1/ai/test") return { ok: false, provider: "ollama", model: "qwen2.5-coder", latency_ms: 3, models_offered: 0, detail: "ConnectError: the provider did not answer" };
      if (call.path === "/projects/p1/secrets") return { available: true, secrets: [] };
      return common(call);
    });
    show(<Models />);
    expect(await screen.findByText("Действует сейчас")).toBeTruthy();
    expect(screen.getAllByText("запрещён Private Mode")).toHaveLength(2);
    expect((screen.getByRole("radio", { name: /^Anthropic/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("switch", { name: /Только локальные модели/ }) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText(/Код не покидает сервер/)).toBeTruthy();
    expect(screen.queryByLabelText(/API-ключ/)).toBeNull(); // Ollama needs no key
    expect((screen.getByLabelText("Адрес сервера моделей") as HTMLInputElement).value).toBe("http://localhost:11434/v1");

    fireEvent.click(screen.getByRole("button", { name: "Проверить подключение" }));
    expect(await screen.findByText("Не удалось подключиться")).toBeTruthy();
    expect(screen.getByText("ConnectError: the provider did not answer")).toBeTruthy();

    // Private Mode off: the cloud providers can be chosen; a new one needs a model.
    fireEvent.click(screen.getByRole("switch", { name: /Только локальные модели/ }));
    fireEvent.click(screen.getByRole("radio", { name: /^Anthropic/ }));
    expect(screen.getByText("Укажите модель для Anthropic.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Сохранить настройки" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "Модель" }), { target: { value: "claude-x" } });
    expect((screen.getByRole("button", { name: "Сохранить настройки" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("asks for a project first", () => {
    serve(() => undefined);
    show(<Models />);
    expect(screen.getByText("Выберите проект")).toBeTruthy();
  });
});
