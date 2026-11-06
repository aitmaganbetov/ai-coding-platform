import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "../data/queries";
import { useUi } from "../shell/store";
import { TooltipProvider } from "../ui";
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

beforeEach(() => {
  queryClient.clear();
  useUi.setState({ projectId: null });
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
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
      if (path.startsWith("/projects/p1/test-runs")) return [];
      return undefined;
    });
    show(<Testing />);
    expect(await screen.findByText("2 high findings")).toBeTruthy();
    const history = screen.getByRole("region", { name: "История Quality Gates" });
    await waitFor(() => expect(within(history).getAllByText("FAIL")).toHaveLength(1));
    expect(within(history).getAllByText("PASS").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("semgrep: app/run.py:4")).toBeTruthy();
    expect(screen.getByText(/в «Тестовых прогонах» ниже/)).toBeTruthy();
    // no run has run tests yet: said plainly, with where tests run
    expect(await screen.findByText("Прогонов с тестами ещё нет")).toBeTruthy();
  });
});

// ------------------------------------------------------------ test runs --
const TOTALS = (tests: number, passed: number, failed: number, skipped: number, time = 1.5) => ({ tests, passed, failed, errors: 0, skipped, time });
const BASE = { state: "COMPLETED", goal: "fix", problems: [], reports: [], fixed: [], regressions: [], compared_with: null, reason: null, setting: null };
const BAD = { key: "tests.test_m::test_div", name: "test_div", classname: "tests.test_m", file: "tests/test_m.py", line: 7, status: "failed", message: "ZeroDivisionError" };
const TEST_RUNS = [
  { ...BASE, run_id: "t3", kind: "develop", created_at: "2026-11-05T10:00:00Z", totals: TOTALS(0, 0, 0, 0, 0), reason: "tests skipped: tests gate disabled", setting: "quality_gates.tests.enabled", failing: [] },
  { ...BASE, run_id: "t2", kind: "develop", created_at: "2026-11-04T10:00:00Z", totals: TOTALS(4, 2, 1, 1), compared_with: "t1", regressions: [BAD], fixed: ["tests.test_m::test_add"], failing: [BAD] },
  { ...BASE, run_id: "t1", kind: "audit", created_at: "2026-11-03T10:00:00Z", totals: TOTALS(3, 2, 1, 0), failing: [] },
];
const case_ = (name: string, status: string, time: number, extra: Record<string, unknown> = {}) => ({
  name,
  classname: "tests.test_m",
  key: `tests.test_m::${name}`,
  file: "tests/test_m.py",
  line: 3,
  time,
  status,
  message: "",
  details: "",
  ...extra,
});
const T2 = {
  ...TEST_RUNS[1],
  failing: undefined,
  reports: [
    { name: "junit-baseline.xml", created_at: null, primary: false, totals: TOTALS(2, 1, 1, 0) },
    { name: "junit-implement.xml", created_at: null, primary: true, totals: TOTALS(4, 2, 1, 1) },
  ],
  suites: [
    {
      name: "pytest",
      tool: "implement",
      tests: 4,
      failures: 1,
      errors: 0,
      skipped: 1,
      time: 1.5,
      cases: [
        case_("test_div", "failed", 0.2, { line: 7, message: "ZeroDivisionError", details: "tests/test_m.py:7: ZeroDivisionError\nTraceback" }),
        case_("test_slow", "passed", 1.1),
        case_("test_add", "passed", 0.05),
        case_("test_later", "skipped", 0, { message: "not yet" }),
      ],
    },
  ],
};

function serveTests() {
  return serve((path) => {
    if (path === "/projects") return PROJECTS;
    if (path.startsWith("/runs?")) return [];
    if (path === "/projects/p1/test-runs?limit=15") return TEST_RUNS;
    if (path === "/runs/t2/tests") return T2;
    if (path === "/runs/t3/tests") return { ...TEST_RUNS[0], failing: undefined, suites: [] };
    if (path.startsWith("/projects/p1/tests/history?test=")) {
      return [
        { run_id: "t2", kind: "develop", created_at: "2026-11-04T10:00:00Z", status: "failed", time: 0.2, message: "ZeroDivisionError" },
        { run_id: "t1", kind: "audit", created_at: "2026-11-03T10:00:00Z", status: "passed", time: 0.1, message: "" },
      ];
    }
    return undefined;
  });
}

describe("test runs", () => {
  it("shows the latest run with results: failed first, regressions, slowest, Code Studio links", async () => {
    useUi.setState({ projectId: "p1" });
    const calls = serveTests();
    show(<Testing />);
    const regressions = await screen.findByRole("region", { name: "Регрессии" });
    expect(within(regressions).getByText(/прошли в прогоне t1/)).toBeTruthy();
    expect(within(regressions).getByText("test_div")).toBeTruthy();
    const open = within(regressions).getByRole("link", { name: /Открыть в Code Studio/ });
    expect(open.getAttribute("href")).toBe("#/projects/p1/code?file=tests%2Ftest_m.py&line=7");
    // the failed test is shown (and only failing ones by default), with its message
    expect(screen.getAllByText("ZeroDivisionError").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("test_add", { selector: ".test-cases .test-name" })).toBeNull();
    expect(screen.getByText(/Починено с прошлого прогона: 1/)).toBeTruthy();
    expect(screen.getByText(/не в итоге: 1\/2 пройдено/)).toBeTruthy();
    // all tests: failed first
    fireEvent.click(screen.getByRole("radio", { name: "Все (4)" }));
    const names = () => [...document.querySelectorAll(".test-cases .test-name")].map((n) => n.firstChild?.textContent);
    await waitFor(() => expect(names()).toEqual(["test_div", "test_slow", "test_add", "test_later"]));
    // the slowest tests, slowest first
    const slow = document.querySelectorAll(".slowest .slow-name");
    expect([...slow].map((n) => n.textContent)).toEqual(["test_slow", "test_div", "test_add"]);
    expect(calls).toContain("/runs/t2/tests");
    expect(calls).not.toContain("/runs/t3/tests"); // the newest run has no results: not opened first
  });

  it("a run without results says why and names the setting", async () => {
    useUi.setState({ projectId: "p1" });
    serveTests();
    show(<Testing />);
    const list = await screen.findByRole("list", { name: /Прогоны с тестами/ });
    expect(within(list).getByText("tests skipped: tests gate disabled")).toBeTruthy();
    fireEvent.click(within(list).getAllByRole("button")[0]!);
    const note = await screen.findByRole("note");
    expect(note.textContent).toContain("Результатов по отдельным тестам нет.");
    expect(note.textContent).toContain("tests skipped: tests gate disabled");
    expect(within(note).getByText("quality_gates.tests.enabled")).toBeTruthy();
    expect(note.textContent).toContain("gate тестов выключен");
  });

  it("opens a test's output and its history across runs", async () => {
    useUi.setState({ projectId: "p1" });
    const calls = serveTests();
    show(<Testing />);
    const more = await screen.findByRole("button", { name: "Подробности и история" });
    fireEvent.click(more);
    expect(await screen.findByLabelText("Вывод теста")).toBeTruthy();
    const history = await screen.findByRole("list", { name: /История теста/ });
    await waitFor(() => expect(within(history).getAllByRole("link")).toHaveLength(2));
    expect(within(history).getByText("пройден")).toBeTruthy();
    expect(calls).toContain(`/projects/p1/tests/history?test=${encodeURIComponent("tests.test_m::test_div")}`);
  });
});
