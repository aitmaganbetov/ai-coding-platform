import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, SecurityFinding, SecurityFindingDetail, SecurityOverview } from "../api";
import { queryClient } from "../data/queries";
import { href, parseRoute, visibleNav } from "../shell/routes";
import { useUi } from "../shell/store";
import { Toaster, TooltipProvider } from "../ui";
import SecurityCenter, { PAGE_SIZE, SECURITY_TOOLS, safeReference } from "./SecurityCenter";

// The Security Center page against a stubbed API: the opt-in switch, real coverage
// states, filters and pagination done by the server, safe details, scanner text shown as
// text, and honest empty and error states.

const ON: Me = { username: "auditor", is_admin: false, features: { security_center: true } };
const OFF: Me = { username: "auditor", is_admin: true, features: { security_center: false } };
const PROJECTS = [
  { id: "p1", name: "alpha", role: "auditor", created_at: "2026-11-01T10:00:00Z" },
  { id: "p2", name: "beta", role: "auditor", created_at: "2026-11-01T10:00:00Z" },
];
const RUNS = [
  { id: "r1", state: "COMPLETED", mode: "AUDIT", goal: "audit", kind: "audit", requested_by: "a", created_at: "2026-11-06T10:00:00Z", finished_at: "2026-11-06T10:05:00Z" },
  { id: "r0", state: "FAILED", mode: "AUDIT", goal: "audit", kind: "audit", requested_by: "a", created_at: "2026-11-05T10:00:00Z", finished_at: null },
];
const XSS = '<img src=x onerror="window.pwned=1">';

function row(over: Partial<SecurityFinding>): SecurityFinding {
  return {
    id: "f1",
    source: "semgrep",
    category: "sast",
    rule_id: "python.eval",
    severity: "high",
    title: "Use of eval",
    file_path: "app/main.py",
    line: 4,
    fingerprint: "0".repeat(64),
    status: "open",
    created_at: "2026-11-06T10:01:00Z",
    ...over,
  };
}

function overview(over: Partial<SecurityOverview> = {}): SecurityOverview {
  const findings = over.findings ?? [
    row({}),
    row({ id: "f2", source: "gitleaks", category: "secrets", title: "Secret detected", rule_id: "github-pat", file_path: ".env", line: 1 }),
    row({ id: "f3", source: "trivy", category: "sca", severity: "critical", title: XSS, rule_id: "CVE-2020-8203", file_path: "package-lock.json", line: null }),
  ];
  return {
    schema_version: "2",
    project_id: "p1",
    run_id: "r1",
    run: { state: "COMPLETED", finished: true, mode: "AUDIT", kind: "audit", created_at: "2026-11-06T10:00:00Z", finished_at: "2026-11-06T10:05:00Z" },
    coverage: {
      sast: { available: true, state: "completed", tools: [{ tool: "semgrep", state: "completed", reason: null }] },
      sca: {
        available: true,
        state: "error",
        tools: [
          { tool: "osv-scanner", state: "skipped", reason: "offline_db_missing" },
          { tool: "trivy", state: "error", reason: "task_failed" },
        ],
      },
      secrets: { available: true, state: "skipped", tools: [{ tool: "gitleaks", state: "skipped", reason: "docker_only" }] },
      iac: { available: true, state: "incomplete", tools: [{ tool: "trivy", state: "running", reason: null }] },
      container: { available: false, state: "unsupported", tools: [] },
      dast: { available: false, state: "unsupported", tools: [] },
    },
    security_gate: { result: "FAIL", blocking: true },
    summary: {
      total: findings.length,
      by_severity: { critical: 1, high: 2, medium: 0, low: 0, info: 0 },
      by_source: { gitleaks: 1, semgrep: 1, trivy: 1 },
      by_status: { open: findings.length },
    },
    findings,
    page: { limit: PAGE_SIZE, offset: 0, total: findings.length, filters: { severity: null, source: null, status: null } },
    security_agent: { state: "not_requested", reason: null },
    launch: { kind: "audit", tools: SECURITY_TOOLS },
    capabilities: { dast: "not_implemented", container: "not_implemented" },
    compliance: { state: "not_assessed", attestation: false },
    ai_fix: { state: "disabled", requires_human_review: true },
    ...over,
  };
}

type Reply = unknown | { status: number; body: unknown };
const isReply = (r: unknown): r is { status: number; body: unknown } => typeof r === "object" && r !== null && "status" in r && "body" in r;

/** Stubs fetch: ``handler`` gets the path with its query string (without /api/v1). */
function serve(handler: (path: string, init?: RequestInit) => Reply | undefined) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace("/api/v1", "");
      calls.push(init?.method && init.method !== "GET" ? `${init.method} ${path}` : path);
      const reply = handler(path, init);
      if (reply === undefined) return new Response(JSON.stringify({ error: { message: `no route ${path}` } }), { status: 404 });
      const { status, body } = isReply(reply) ? reply : { status: 200, body: reply };
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return calls;
}

function api(over: { overview?: (path: string) => Reply; runs?: unknown[] | (() => unknown[]); projects?: unknown[]; detail?: SecurityFindingDetail; start?: (body: unknown) => Reply } = {}) {
  return serve((path, init) => {
    if (init?.method === "POST" && /^\/projects\/p\d\/runs$/.test(path)) return over.start?.(JSON.parse(String(init.body)));
    if (path === "/projects") return over.projects ?? PROJECTS;
    if (/^\/projects\/p\d\/runs$/.test(path)) return typeof over.runs === "function" ? over.runs() : (over.runs ?? RUNS);
    if (path.includes("/security-center/runs/") && path.includes("/findings/")) return over.detail;
    if (path.includes("/security-center/runs/")) return (over.overview ?? (() => overview()))(path);
    return undefined;
  });
}

function show(ui: ReactNode) {
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        {ui}
        <Toaster />
      </TooltipProvider>
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

describe("security center switch", () => {
  it("is hidden from navigation and loads nothing while the server has it off", async () => {
    expect(visibleNav(OFF).map((i) => i.id)).not.toContain("security");
    expect(visibleNav({ username: "u", is_admin: false }).map((i) => i.id)).not.toContain("security"); // older servers
    expect(visibleNav(ON).map((i) => i.id)).toContain("security");
    const calls = api();
    show(<SecurityCenter me={OFF} query={{}} />);
    expect(await screen.findByText("Модуль выключен на этом сервере")).toBeTruthy();
    expect(calls).toEqual([]);
  });

  it("has its own route", () => {
    expect(parseRoute("#/security")).toEqual({ view: "security" });
    expect(parseRoute("#/security?project=p1&run=r1")).toEqual({ view: "security", query: { project: "p1", run: "r1" } });
    expect(href("security")).toBe("#/security");
  });
});

describe("security center overview", () => {
  it("shows real coverage states, the gate and what is not implemented", async () => {
    api();
    show(<SecurityCenter me={ON} query={{}} />);
    const coverage = await screen.findByRole("region", { name: "Покрытие сканирования" });
    const card = (name: string) => within(coverage).getByText(name).closest("section") as HTMLElement;
    expect(within(card("SAST")).getByText("Выполнено")).toBeTruthy();
    expect(within(card("SCA — зависимости")).getByText("Ошибка")).toBeTruthy();
    expect(within(card("SCA — зависимости")).getByText(/нет офлайн-базы уязвимостей/)).toBeTruthy();
    expect(within(card("SCA — зависимости")).getByText(/задача run'а упала/)).toBeTruthy();
    expect(within(card("Секреты")).getByText("Пропущено")).toBeTruthy();
    expect(within(card("IaC")).getByText("Неполное")).toBeTruthy();
    expect(within(card("IaC")).getByText("выполняется")).toBeTruthy();
    expect(within(card("Контейнеры")).getByText("Не реализовано")).toBeTruthy();
    expect(within(card("DAST")).getByText("Не реализовано")).toBeTruthy();
    expect(screen.getByText(/«Выполнено» не означает PASS/)).toBeTruthy();
    expect(screen.getByText("FAIL")).toBeTruthy();
    expect(screen.getByText("не оценивалось")).toBeTruthy();
    expect(screen.getByText(/выключено/)).toBeTruthy();
    expect(screen.getByText("Находки run'а по важности · всего 3")).toBeTruthy();
  });

  it("renders scanner text as text, never as HTML", async () => {
    api();
    const { container } = show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText(XSS)).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { pwned?: number }).pwned).toBeUndefined();
  });

  it("filters and pages on the server", async () => {
    // The server pages: the first page here carries a few rows of a larger total.
    const first = [row({ id: "a", title: "first page" }), row({ id: "b", title: "first page too" })];
    const calls = api({
      overview: (path) => {
        const offset = Number(new URLSearchParams(path.split("?")[1]).get("offset"));
        const page = offset === 0 ? first : [row({ id: "last", title: "the last one" })];
        return overview({ findings: page, page: { limit: PAGE_SIZE, offset, total: PAGE_SIZE + 1, filters: { severity: null, source: null, status: null } } });
      },
    });
    show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText(`1–2 из ${PAGE_SIZE + 1}`)).toBeTruthy();
    expect((screen.getByRole("button", { name: /Назад/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /Вперёд/ }));
    expect(await screen.findByText("the last one")).toBeTruthy();
    expect(calls.some((c) => c.includes(`offset=${PAGE_SIZE}`))).toBe(true);
    expect((screen.getByRole("button", { name: /Вперёд/ }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Важность"), { target: { value: "critical" } });
    await waitFor(() => expect(calls.at(-1)).toContain("severity=critical"));
    expect(calls.at(-1)).toContain("offset=0"); // a new filter starts at the first page
    fireEvent.change(screen.getByLabelText("Инструмент"), { target: { value: "trivy" } });
    await waitFor(() => expect(calls.at(-1)).toMatch(/severity=critical.*source=trivy|source=trivy.*severity=critical/));
  });

  it("shows only the safe details of a finding", async () => {
    api({
      detail: {
        ...row({ id: "f3", source: "trivy", category: "sca", severity: "critical", title: "lodash", rule_id: "CVE-2020-8203" }),
        details: { package: "lodash", installed_version: "4.17.15", fixed_version: "4.17.19", aliases: ["GHSA-p6mc-m468-83gw"], reference: "https://avd.aquasec.com/nvd/cve-2020-8203" },
        withheld: ["scanner_message", "raw_evidence", "code_snippet", "raw_output"],
      },
    });
    show(<SecurityCenter me={ON} query={{}} />);
    fireEvent.click(await screen.findByText(XSS));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("4.17.19")).toBeTruthy();
    expect(within(dialog).getByText("GHSA-p6mc-m468-83gw")).toBeTruthy();
    const link = within(dialog).getByRole("link", { name: "https://avd.aquasec.com/nvd/cve-2020-8203" });
    expect(link.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(within(dialog).getByText(/Сообщение сканера, фрагменты кода и исходные доказательства не выдаются/)).toBeTruthy();
  });

  it("says why a secret shows no value", async () => {
    api({ detail: { ...row({ id: "f2", source: "gitleaks", category: "secrets", title: "Secret detected" }), details: {}, withheld: ["raw_evidence"] } });
    show(<SecurityCenter me={ON} query={{}} />);
    fireEvent.click(await screen.findByText("Secret detected"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Значение секрета, совпадение и сообщение сканера не выдаются/)).toBeTruthy();
  });

  it("only links plain https references", () => {
    expect(safeReference("https://osv.dev/vuln/GHSA-1")).toBe("https://osv.dev/vuln/GHSA-1");
    for (const bad of ["javascript:alert(1)", "http://example.org", "https://user:pw@example.org/", "data:text/html,x", "not a url", undefined]) {
      expect(safeReference(bad)).toBeNull();
    }
  });
});

describe("security center empty and error states", () => {
  it("never calls an empty run secure", async () => {
    api({ overview: () => overview({ findings: [] }) });
    show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText("В этом run'е находок нет")).toBeTruthy();
    expect(screen.getByText(/Это не означает, что проект безопасен/)).toBeTruthy();
  });

  it("explains an unfinished run", async () => {
    api({ overview: () => overview({ run: { ...overview().run, state: "RUNNING", finished: false } }) });
    show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText(/Run ещё выполняется \(RUNNING\)/)).toBeTruthy();
  });

  it("handles projects without runs and users without projects", async () => {
    api({ runs: [] });
    show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText("У проекта ещё нет run'ов")).toBeTruthy();
    cleanup();
    queryClient.clear();
    api({ projects: [] });
    show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText("Нет доступных проектов")).toBeTruthy();
  });

  it.each([
    [404, /Run не найден, у вас нет доступа к проекту или модуль выключен/],
    [403, /Недостаточно прав/],
    [500, /boom/],
  ])("reports an API error %i", async (status, text) => {
    queryClient.setQueryDefaults(["security-center"], { retry: false }); // no back-off wait
    api({ overview: () => ({ status, body: { error: { message: "boom" } } }) });
    show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText("Обзор безопасности недоступен")).toBeTruthy();
    expect(screen.getByText(text)).toBeTruthy();
  });

  it("switches runs and projects", async () => {
    const calls = api();
    show(<SecurityCenter me={ON} query={{ project: "p1", run: "r0" }} />);
    await screen.findByRole("region", { name: "Покрытие сканирования" });
    expect(calls.some((c) => c.startsWith("/projects/p1/security-center/runs/r0"))).toBe(true);
    fireEvent.change(screen.getByLabelText("Run"), { target: { value: "r1" } });
    await waitFor(() => expect(calls.some((c) => c.startsWith("/projects/p1/security-center/runs/r1"))).toBe(true));
    fireEvent.change(screen.getByLabelText("Проект"), { target: { value: "p2" } });
    await waitFor(() => expect(calls.some((c) => c.startsWith("/projects/p2/security-center/runs/r1"))).toBe(true));
    expect(useUi.getState().projectId).toBe("p2");
  });
});

const DEV_PROJECTS = [{ ...PROJECTS[0], role: "developer" }];

describe("security audit and the Security Agent", () => {
  it("starts a security audit with the agent through the runs API", async () => {
    const started: unknown[] = [];
    let runs: unknown[] = RUNS;
    const calls = api({
      projects: DEV_PROJECTS,
      runs: () => runs,
      start: (body) => {
        started.push(body);
        runs = [{ ...RUNS[0], id: "r9", state: "RUNNING", finished_at: null }, ...RUNS];
        return { status: 201, body: { ...RUNS[0], id: "r9", state: "PLANNED" } };
      },
    });
    show(<SecurityCenter me={ON} query={{}} />);
    await screen.findByRole("region", { name: "Покрытие сканирования" });
    fireEvent.click(screen.getByRole("button", { name: "Запустить аудит безопасности" }));
    const dialog = await screen.findByRole("dialog");
    for (const tool of SECURITY_TOOLS) expect(within(dialog).getByText(tool)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Запустить" }));
    await waitFor(() => expect(started).toEqual([{ kind: "audit", params: { tools: SECURITY_TOOLS, triage: true } }]));
    expect(await screen.findByText("Аудит безопасности запущен")).toBeTruthy();
    await waitFor(() => expect(calls.some((c) => c.startsWith("/projects/p1/security-center/runs/r9"))).toBe(true));
  });

  it("leaves the start to roles that may run", async () => {
    api(); // the default project role is auditor
    show(<SecurityCenter me={ON} query={{}} />);
    const button = await screen.findByRole("button", { name: "Запустить аудит безопасности" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("reports a start the server refuses", async () => {
    api({ projects: DEV_PROJECTS, start: () => ({ status: 403, body: { error: { message: "forbidden" } } }) });
    show(<SecurityCenter me={ON} query={{}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Запустить аудит безопасности" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Запустить" }));
    expect(await screen.findByText("Аудит не запущен")).toBeTruthy();
    expect(screen.getByText(/Недостаточно прав/)).toBeTruthy();
  });

  it("shows the agent's verdicts and extra risks as text", async () => {
    const agent = {
      state: "completed" as const,
      reason: null,
      verdicts: { confirmed: 1, false_positive: 2, needs_review: 3 },
      triaged: 6,
      total: 9,
      models: ["anthropic/claude"],
      summary: "Checked.\nSecond line",
      additional_risks: [{ file: "app/auth.py", line: 7, severity: "high", description: XSS, recommendation: "Use hmac.compare_digest." }],
    };
    const findings = [row({ agent_verdict: "confirmed" }), row({ id: "f2", title: "other", agent_verdict: null })];
    api({ overview: () => overview({ security_agent: agent, findings }) });
    const { container } = show(<SecurityCenter me={ON} query={{}} />);
    const card = (await screen.findByText("Security Agent — AI-проверка находок")).closest("section") as HTMLElement;
    expect(within(card).getByText("Готово")).toBeTruthy();
    expect(within(card).getByText(/Проверено 6 из 9 находок · anthropic\/claude/)).toBeTruthy();
    expect(within(card).getByText("app/auth.py:7")).toBeTruthy();
    expect(within(card).getByText(XSS)).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
    const table = screen.getByRole("region", { name: "Находки run'а" });
    expect(within(table).getByText("подтверждена")).toBeTruthy();
  });

  it.each([
    [{ state: "not_requested", reason: null }, /AI-проверка не запрашивалась/],
    [{ state: "awaiting_approval", reason: null }, /ждёт утверждения/],
    [{ state: "running", reason: null }, /Агент проверяет находки/],
    [{ state: "error", reason: "ai_disabled" }, /AI выключен в настройках проекта/],
    [{ state: "skipped", reason: "no_new_findings" }, /Новых находок для проверки нет/],
  ])("explains the agent state %j", async (agent, text) => {
    api({ overview: () => overview({ security_agent: agent as SecurityOverview["security_agent"] }) });
    show(<SecurityCenter me={ON} query={{}} />);
    expect(await screen.findByText(text)).toBeTruthy();
  });

  it("links the approval of an external model to the run page", async () => {
    api({ overview: () => overview({ security_agent: { state: "awaiting_approval", reason: null } }) });
    show(<SecurityCenter me={ON} query={{}} />);
    const card = (await screen.findByText("Security Agent — AI-проверка находок")).closest("section") as HTMLElement;
    expect(within(card).getByRole("link", { name: "странице run'а" }).getAttribute("href")).toBe("#/runs/r1");
  });

  it("shows the agent's assessment in the details, without code", async () => {
    api({
      detail: {
        ...row({}),
        details: {},
        withheld: ["agent_evidence"],
        agent: { verdict: "confirmed", severity: "critical", cwe: "CWE-95", exploit_scenario: "Input reaches eval().", recommendation: "Use literal_eval.\nAdd a test." },
      },
    });
    show(<SecurityCenter me={ON} query={{}} />);
    fireEvent.click(await screen.findByText("Use of eval"));
    const section = await within(await screen.findByRole("dialog")).findByRole("region", { name: "Оценка Security Agent" });
    expect(within(section).getByText("подтверждена")).toBeTruthy();
    expect(within(section).getByText("Input reaches eval().")).toBeTruthy();
    expect(within(section).getByText(/Use literal_eval/).textContent).toBe("Use literal_eval.\nAdd a test.");
    expect(within(section).getByText(/Цитаты кода из ответа агента не показываются/)).toBeTruthy();
  });
});
