import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FindingRow } from "../api";
import { queryClient } from "../data/queries";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
import AuditCenter, { categoryOf, compareAudits, fixDraft } from "./AuditCenter";

// Recharts measures its container; jsdom has no layout: give charts a fixed size.
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children) ? cloneElement(children as ReactElement<{ width: number; height: number }>, { width: 600, height: 220 }) : null,
  };
});

type Handler = (path: string, init?: RequestInit) => unknown;
function serve(handler: Handler) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace("/api/v1", "");
      calls.push(`${init?.method ?? "GET"} ${path}`);
      const body = handler(path, init);
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

// Two audits of alpha. Every audit records its findings anew: the API's "current" findings
// are those of the latest one (r2), with the ones its gates accepted through the baseline.
const RUNS = [
  { id: "r2", project_id: "p1", project_name: "alpha", state: "COMPLETED", mode: "audit", kind: "audit", goal: "audit", requested_by: "dev", created_at: "2026-11-06T10:00:00Z", finished_at: "2026-11-06T10:05:00Z" },
  { id: "r1", project_id: "p1", project_name: "alpha", state: "COMPLETED", mode: "audit", kind: "audit", goal: "audit", requested_by: "dev", created_at: "2026-11-05T10:00:00Z", finished_at: "2026-11-05T10:05:00Z" },
];
const IN_LATEST = [
  finding({ id: "c1", source: "semgrep", severity: "high", title: "SQL injection", file_path: "app/db.py", line: 9, fingerprint: "sqli" }),
  finding({ id: "c2", source: "osv-scanner", severity: "high", title: "requests 2.0 CVE", file_path: "requirements.txt", fingerprint: "cve" }),
  finding({ id: "c3", source: "ruff", severity: "low", title: "Unused import", file_path: "app/db.py", line: 1, fingerprint: "f401" }),
  finding({ id: "c4", source: "playwright", severity: "medium", title: "Button without a name", fingerprint: "a11y" }),
  finding({ id: "c5", source: "agent.database", severity: "medium", title: "Missing index on orders", fingerprint: "idx" }),
  finding({ id: "c6", source: "architect", severity: "low", title: "Cycle between modules", fingerprint: "cycle" }),
  finding({ id: "c7", source: "custom-check", severity: "info", title: "Something else", fingerprint: "other" }),
  finding({ id: "c8", source: "trivy", severity: "high", title: "Planted CVE in a fixture", file_path: "tests/fixtures/composer.lock", fingerprint: "planted", status: "baselined" }),
];
const IN_FIRST = [
  finding({ id: "h1", run_id: "r1", source: "osv-scanner", severity: "high", title: "requests 2.0 CVE", file_path: "requirements.txt", fingerprint: "cve", created_at: "2026-11-05T10:01:00Z" }),
  finding({ id: "h2", run_id: "r1", source: "gitleaks", severity: "critical", title: "Leaked key", file_path: "config.py", fingerprint: "leak", created_at: "2026-11-05T10:01:00Z" }),
];

function auditServer() {
  return serve((path) => {
    if (path === "/projects") return PROJECTS;
    if (path.startsWith("/runs")) return RUNS;
    if (path === "/projects/p1/runs") return { id: "r3" };
    if (!path.startsWith("/findings")) return undefined;
    const q = new URLSearchParams(path.split("?")[1] ?? "");
    if (q.get("run_id") === "r1") return IN_FIRST;
    if (q.get("run_id") === "r2") return IN_LATEST;
    return q.get("current") === "true" ? IN_LATEST : [...IN_LATEST, ...IN_FIRST];
  });
}

const tab = (name: RegExp) => within(screen.getByRole("tablist", { name: "Категории аудита" })).getByRole("tab", { name });

describe("audit center tabs", () => {
  it("counts the latest audit in the overview, not every audit's copy; the trend shows every audit", async () => {
    const calls = auditServer();
    show(<AuditCenter query={{ project: "p1" }} />);
    await screen.findByText("SQL injection");
    expect(calls).toContain("GET /findings?limit=1000&project_id=p1&current=true");
    const stat = (label: string) => screen.getByText(label, { selector: ".stat-label" }).previousSibling?.textContent;
    // r1's copy of the CVE is not counted again; the baselined one is not open.
    expect([stat("critical"), stat("high"), stat("medium"), stat("low"), stat("info")]).toEqual(["0", "2", "2", "2", "1"]);
    expect(screen.getByText(/По последнему аудиту каждого проекта · ещё 1 принято через baseline/)).toBeTruthy();
    const categories = screen.getByText("По категориям").closest(".panel") as HTMLElement;
    const cell = (name: string) => within(categories).getByRole("button", { name: new RegExp(name) }).querySelector(".cat-count")?.textContent;
    expect([cell("Качество кода"), cell("Безопасность"), cell("Зависимости"), cell("База данных"), cell("Производительность"), cell("Архитектура"), cell("Доступность")]).toEqual(["1", "1", "1", "1", "0", "1", "1"]); // open ones: not the baselined
    expect(within(categories).getByText("Прочее")).toBeTruthy();
    const files = screen.getByText("Файлы с наибольшим числом находок").closest(".panel") as HTMLElement;
    expect(within(files).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["app/db.py2", "requirements.txt1"]);
    const bars = within(screen.getByRole("list", { name: "Число находок по аудитам" })).getAllByRole("listitem");
    expect(bars.map((b) => [b.getAttribute("href"), b.textContent])).toEqual([["#/runs/r1", "2"], ["#/runs/r2", "8"]]);
    expect(screen.getByRole("heading", { name: "Находки последнего аудита · 8" })).toBeTruthy();
    expect(screen.queryByText("Leaked key")).toBeNull(); // fixed since r1

    // A category of the overview opens its tab.
    fireEvent.click(within(categories).getByRole("button", { name: /Безопасность/ }));
    expect(tab(/Безопасность/).getAttribute("aria-selected")).toBe("true");
  });

  it("lists each category's findings in its tab, or says where they come from", async () => {
    auditServer();
    show(<AuditCenter query={{ project: "p1" }} />);
    await screen.findByText("SQL injection");
    const expected: [RegExp, string[]][] = [
      [/Качество кода/, ["Unused import"]],
      [/Безопасность/, ["SQL injection"]],
      [/Зависимости/, ["Planted CVE in a fixture", "requests 2.0 CVE"]],
      [/База данных/, ["Missing index on orders"]],
      [/Архитектура/, ["Cycle between modules"]],
      [/Доступность/, ["Button without a name"]],
    ];
    for (const [name, titles] of expected) {
      fireEvent.click(tab(name));
      expect(tab(name).querySelector(".tab-count")?.textContent).toBe(String(titles.length));
      const table = screen.getByRole("region", { name: "Находки" });
      const shown = within(table).getAllByRole("row").slice(1).map((r) => r.querySelector(".cell-main")?.textContent);
      expect(shown.sort()).toEqual([...titles].sort());
    }
    fireEvent.click(tab(/Производительность/));
    expect(screen.getByText("Производительность: находок нет")).toBeTruthy();
    expect(screen.getByText(/Их даёт k6/)).toBeTruthy();
    // The baselined finding stays visible, with its status.
    fireEvent.click(tab(/Зависимости/));
    const planted = screen.getByText("Planted CVE in a fixture").closest("tr") as HTMLElement;
    expect(within(planted).getByText("baselined")).toBeTruthy();
  });

  it("filters by status, tool and text; all projects show the project's name", async () => {
    auditServer();
    show(<AuditCenter query={{}} />);
    await screen.findByText("SQL injection");
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toContain("Проект");
    const select = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
    select("Статус", "baselined");
    expect(screen.getByRole("heading", { name: "Находки последнего аудита · 1" })).toBeTruthy();
    expect(screen.getByText("Planted CVE in a fixture")).toBeTruthy();
    select("Статус", "");
    select("Инструмент", "ruff");
    expect(screen.getByRole("heading", { name: "Находки последнего аудита · 1" })).toBeTruthy();
    expect(screen.getByText("Unused import")).toBeTruthy();
    select("Инструмент", "");
    fireEvent.change(screen.getByLabelText("Поиск по файлу, правилу и тексту"), { target: { value: "app/db.py" } });
    expect(screen.getByRole("heading", { name: "Находки последнего аудита · 2" })).toBeTruthy();
    expect(tab(/Безопасность/).querySelector(".tab-count")?.textContent).toBe("1"); // the counters follow
    expect(tab(/Зависимости/).querySelector(".tab-count")?.textContent).toBe("0");
  });

  it("reports: the project's audits, and what changed between two of them", async () => {
    auditServer();
    show(<AuditCenter query={{ project: "p1", category: "reports" }} />);
    const audits = await screen.findByRole("region", { name: "Аудиты" });
    expect(within(audits).getAllByRole("row")).toHaveLength(3); // the header and two audits
    expect(await screen.findByRole("region", { name: "Исправленные" })).toBeTruthy();
    const list = (name: string) => within(screen.getByRole("region", { name })).getAllByRole("listitem").map((li) => li.querySelector(".truncate")?.textContent);
    expect(list("Исправленные")).toEqual(["Leaked key"]);
    expect(list("Сохранившиеся")).toEqual(["requests 2.0 CVE"]);
    expect(list("Новые")).toHaveLength(7);
    fireEvent.change(screen.getByLabelText("Аудит A (раньше)"), { target: { value: "r2" } });
    expect(screen.getByText("Выберите два разных аудита.")).toBeTruthy();
    fireEvent.click(within(audits).getAllByRole("row")[2] as HTMLElement);
    expect(window.location.hash).toBe("#/runs/r1");
  });

  it("asks for a project before reports and a full audit; starts the audit of the chosen one", async () => {
    const calls = auditServer();
    show(<AuditCenter query={{ category: "reports" }} />);
    expect(await screen.findByText("Выберите проект")).toBeTruthy();
    expect((screen.getByRole("button", { name: /Полный аудит/ }) as HTMLButtonElement).disabled).toBe(true);
    await screen.findByRole("option", { name: "alpha" });
    fireEvent.change(screen.getByLabelText("Проект"), { target: { value: "p1" } });
    const start = screen.getByRole("button", { name: "Полный аудит «alpha»" });
    fireEvent.click(start);
    await waitFor(() => expect(window.location.hash).toBe("#/runs/r3")); // the new run's page
    expect(calls).toContain("POST /projects/p1/runs");
  });
});
