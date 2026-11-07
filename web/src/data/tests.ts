import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../api";
import { href } from "../shell/routes";

// Per-test results of runs from their JUnit reports (ADR-0044). The sandboxed tests of
// DEVELOP / MODERNIZE / MAINTAIN and the audit's test tools (go test, cargo test) leave
// reports; a run without one says why (`reason`) and which setting decides it (`setting`).

export type CaseStatus = "passed" | "failed" | "error" | "skipped";

export interface TestCase {
  name: string;
  classname: string;
  key: string; // classname::name — the test's identity across runs
  file: string | null; // relative to the project
  line: number | null; // 1-based; the failing line for a failed test when known
  time: number; // seconds
  status: CaseStatus;
  message: string;
  details: string; // clipped (~4 KB)
}

export interface TestSuite {
  name: string;
  tool: string;
  tests: number;
  failures: number;
  errors: number;
  skipped: number;
  time: number;
  cases: TestCase[]; // failed first, then the slowest
}

export interface TestTotals {
  tests: number;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  time: number;
}

export interface TestReport {
  name: string; // junit-<task or tool>.xml
  created_at: string | null;
  primary: boolean; // counted in the run's results (the others are listed only)
  totals?: TestTotals; // absent when the report could not be read
}

export type BriefCase = Pick<TestCase, "key" | "name" | "classname" | "file" | "line" | "status" | "message">;

interface RunTestsBase {
  run_id: string;
  kind: string | null;
  state: string;
  goal: string;
  created_at: string | null;
  totals: TestTotals;
  reports: TestReport[];
  problems: string[];
  reason: string | null; // why there are no results
  setting: string | null; // the setting behind the reason
  compared_with: string | null; // the previous run with results
  regressions: BriefCase[]; // passed there, fail now
  fixed: string[]; // keys: failed there, pass now
}

export interface RunTests extends RunTestsBase {
  suites: TestSuite[];
}

export interface TestRunSummary extends RunTestsBase {
  failing: BriefCase[];
}

export interface TestHistoryPoint {
  run_id: string;
  kind: string | null;
  created_at: string | null;
  status: CaseStatus | "absent";
  time: number | null;
  message: string;
}

const enc = encodeURIComponent;

export const testsApi = {
  run: (runId: string) => apiRequest<RunTests>("GET", `/runs/${enc(runId)}/tests`),
  projectRuns: (projectId: string, limit = 15) => apiRequest<TestRunSummary[]>("GET", `/projects/${enc(projectId)}/test-runs?limit=${limit}`),
  history: (projectId: string, key: string) => apiRequest<TestHistoryPoint[]>("GET", `/projects/${enc(projectId)}/tests/history?test=${enc(key)}`),
};

export function useTestRuns(projectId: string | null | undefined) {
  return useQuery({
    queryKey: ["test-runs", projectId ?? ""],
    queryFn: () => testsApi.projectRuns(projectId as string),
    enabled: Boolean(projectId),
    staleTime: 30_000,
  });
}

export function useRunTests(runId: string | null | undefined) {
  return useQuery({ queryKey: ["run-tests", runId ?? ""], queryFn: () => testsApi.run(runId as string), enabled: Boolean(runId), staleTime: 60_000 });
}

export function useTestHistory(projectId: string | null | undefined, key: string | null) {
  return useQuery({
    queryKey: ["test-history", projectId ?? "", key ?? ""],
    queryFn: () => testsApi.history(projectId as string, key as string),
    enabled: Boolean(projectId && key),
    staleTime: 60_000,
  });
}

// ---------------------------------------------------------------- helpers --
export const STATUS_LABEL: Record<CaseStatus | "absent", string> = {
  passed: "пройден",
  failed: "упал",
  error: "ошибка",
  skipped: "пропущен",
  absent: "не было",
};

export const KIND_LABEL: Record<string, string> = {
  develop: "Разработка",
  modernize: "Модернизация",
  maintain: "Сопровождение",
  audit: "Аудит",
};

/** What to change when a run has no per-test results, by the setting the API named. */
export const SETTING_HINT: Record<string, string> = {
  "quality_gates.tests.enabled": "gate тестов выключен — включите его в настройках качества проекта.",
  "quality_gates.tests.command":
    "команда тестов не пишет JUnit. Платформа сама добавляет отчёт для pytest, node --test и Vitest; своя команда должна писать его в /deps/junit.xml (Jest — через jest-junit).",
  "execution.docker.network": "установке зависимостей нужна сеть песочницы: execution.docker.network = bridge.",
  "quality_gates.tests.install_hosts": "хосты установки зависимостей (PyPI, npm) должны быть в install_hosts и разрешены сетевой политикой (egress).",
  "execution.backend": "тесты идут только в песочнице Docker — она недоступна на сервере.",
  "execution.docker": "образа песочницы нет на сервере: ai-coding tools pull.",
};

export const failedCount = (t: TestTotals): number => t.failed + t.errors;

export function isFailing(status: string): boolean {
  return status === "failed" || status === "error";
}

/** The slowest tests of a run (all suites). */
export function slowest(suites: TestSuite[], n = 10): (TestCase & { suite: string })[] {
  return suites
    .flatMap((s) => s.cases.map((c) => ({ ...c, suite: s.name })))
    .filter((c) => c.time > 0)
    .sort((a, b) => b.time - a.time)
    .slice(0, n);
}

/** Code Studio at the test's file and line (null when the report names no project file). */
export function codeHref(projectId: string, c: Pick<TestCase, "file" | "line">): string | null {
  if (!c.file) return null;
  return href("code", projectId, { file: c.file, line: c.line ? String(c.line) : undefined });
}

export function seconds(value: number): string {
  if (value <= 0) return "—";
  if (value < 1) return `${Math.round(value * 1000)} мс`;
  if (value < 60) return `${value.toFixed(value < 10 ? 2 : 1)} с`;
  return `${Math.floor(value / 60)} мин ${Math.round(value % 60)} с`;
}
