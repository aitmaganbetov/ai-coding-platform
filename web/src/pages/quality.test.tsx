import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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
