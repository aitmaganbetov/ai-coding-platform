import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../api";
import { queryClient } from "./queries";

// GitHub Actions of a project's repository, through the server (ADR-0043). The token is
// the write-only project secret "github.token" (SecretField): no answer carries it.

export interface GitHubConfig {
  enabled?: boolean;
  repository?: string;
  api_url?: string;
  default_ref?: string;
}

/** Why a call did not work (the server also sends its own text). */
export type GitHubReason =
  | "no_repository"
  | "no_token"
  | "invalid_settings"
  | "disabled"
  | "bad_token"
  | "forbidden"
  | "not_found"
  | "refused"
  | "rate_limited"
  | "unreachable"
  | "upstream";

export interface GitHubLast {
  ok: boolean;
  error: string | null;
  reason: GitHubReason | null;
  kind: "check" | "poll";
  at: string;
}

export interface GitHubSettings {
  data: GitHubConfig;
  version: number;
  updated_by: string | null;
  updated_at: string | null;
  enabled: boolean;
  token_set: boolean;
  token_hint: string | null; // only for those who manage the project
  can_manage: boolean;
  last: GitHubLast | null;
}

export interface GitHubCheck {
  ok: boolean;
  checked_at: string;
  error?: string;
  reason?: GitHubReason;
  repository?: string;
  private?: boolean;
  default_branch?: string;
  html_url?: string;
  permissions?: Record<string, boolean>;
}

export interface GitHubWorkflow {
  id: number;
  name: string;
  path: string;
  file: string;
  state: string;
  html_url: string;
}

export interface GitHubRun {
  id: number;
  name: string | null;
  title: string | null;
  workflow_id: number | null;
  path: string | null;
  status: string | null; // queued, in_progress, completed…
  conclusion: string | null; // success, failure, cancelled…
  branch: string | null;
  sha: string | null;
  event: string | null;
  actor: string | null;
  run_number: number | null;
  started_at: string | null;
  updated_at: string | null;
  duration_seconds: number | null;
  html_url: string | null;
}

export interface GitHubStep {
  number: number;
  name: string;
  status: string | null;
  conclusion: string | null;
}

export interface GitHubJob {
  id: number;
  name: string;
  status: string | null;
  conclusion: string | null;
  started_at: string | null;
  completed_at: string | null;
  html_url: string | null;
  steps: GitHubStep[];
}

export interface GitHubProjectStatus {
  project_id: string;
  name: string;
  repository: string | null;
  enabled: boolean;
  token_set: boolean;
  connected: boolean;
  last: GitHubLast | null;
  can_manage: boolean;
}

export interface GitHubStatus {
  projects: GitHubProjectStatus[];
  connected: number;
  enabled: number;
  failing: number;
}

const base = (projectId: string) => `/projects/${encodeURIComponent(projectId)}/github`;

export const githubApi = {
  settings: (projectId: string) => apiRequest<GitHubSettings>("GET", base(projectId)),
  save: (projectId: string, config: GitHubConfig, version?: number) =>
    apiRequest<{ data: GitHubConfig; version: number }>("PUT", base(projectId), { ...config, ...(version !== undefined ? { version } : {}) }),
  check: (projectId: string) => apiRequest<GitHubCheck>("GET", `${base(projectId)}/check`),
  workflows: (projectId: string) => apiRequest<GitHubWorkflow[]>("GET", `${base(projectId)}/workflows`),
  runs: (projectId: string, filter: { workflow?: string; branch?: string; limit?: number } = {}) => {
    const q = new URLSearchParams(Object.entries(filter).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]));
    const query = q.toString();
    return apiRequest<GitHubRun[]>("GET", `${base(projectId)}/runs${query ? `?${query}` : ""}`);
  },
  jobs: (projectId: string, runId: number) => apiRequest<GitHubJob[]>("GET", `${base(projectId)}/runs/${runId}/jobs`),
  dispatch: (projectId: string, workflow: string, ref: string, inputs: Record<string, string> = {}) =>
    apiRequest<{ ok: boolean; workflow: string; ref: string }>("POST", `${base(projectId)}/workflows/${encodeURIComponent(workflow)}/dispatch`, { ref, inputs }),
  status: () => apiRequest<GitHubStatus>("GET", "/github/status"),
};

export const githubKey = (projectId: string) => ["github", projectId] as const;

export const useGitHubSettings = (projectId: string | null | undefined) =>
  useQuery({ queryKey: githubKey(projectId ?? ""), queryFn: () => githubApi.settings(projectId as string), enabled: Boolean(projectId) });

export const useGitHubStatus = () => useQuery({ queryKey: ["github-status"], queryFn: githubApi.status, refetchInterval: 60_000, retry: false });

const ACTIVE = new Set(["queued", "in_progress", "waiting", "requested", "pending"]);

export const useGitHubRuns = (projectId: string | null | undefined, ready: boolean, limit = 15) =>
  useQuery({
    queryKey: [...githubKey(projectId ?? ""), "runs", limit],
    queryFn: () => githubApi.runs(projectId as string, { limit }),
    enabled: Boolean(projectId) && ready,
    retry: false,
    // A run in progress is followed closely; otherwise GitHub is asked rarely.
    refetchInterval: (q) => (q.state.data?.some((r) => ACTIVE.has(r.status ?? "")) ? 15_000 : 60_000),
  });

export const useGitHubWorkflows = (projectId: string | null | undefined, ready: boolean) =>
  useQuery({
    queryKey: [...githubKey(projectId ?? ""), "workflows"],
    queryFn: () => githubApi.workflows(projectId as string),
    enabled: Boolean(projectId) && ready,
    retry: false,
    staleTime: 5 * 60_000,
  });

export const useGitHubJobs = (projectId: string, runId: number | null) =>
  useQuery({
    queryKey: [...githubKey(projectId), "jobs", runId],
    queryFn: () => githubApi.jobs(projectId, runId as number),
    enabled: runId !== null,
    retry: false,
  });

export function refreshGitHub(projectId: string): void {
  void queryClient.invalidateQueries({ queryKey: githubKey(projectId) });
  void queryClient.invalidateQueries({ queryKey: ["github-status"] });
}

/** What to tell a person, in Russian, for a reason; the server's own text follows it. */
export const REASON_TEXT: Record<GitHubReason, string> = {
  no_repository: "Не указан репозиторий GitHub.",
  no_token: "Не задан токен GitHub.",
  invalid_settings: "Настройки GitHub непригодны (адрес API).",
  disabled: "GitHub Actions выключены для проекта.",
  bad_token: "GitHub отклонил токен: он неверен, истёк или отозван.",
  forbidden: "Токену не хватает прав на репозиторий (Actions, Contents).",
  not_found: "Репозиторий не найден, или токен его не видит.",
  refused: "GitHub отклонил запрос.",
  rate_limited: "Превышен лимит запросов GitHub — повторите позже.",
  unreachable: "GitHub недоступен с сервера.",
  upstream: "GitHub ответил ошибкой.",
};

export function reasonText(reason: GitHubReason | null | undefined, fallback?: string | null): string {
  return (reason && REASON_TEXT[reason]) || fallback || "Неизвестная ошибка.";
}

/** A run's state for a badge. */
export function runTone(run: Pick<GitHubRun, "status" | "conclusion">): { label: string; tone: "success" | "danger" | "warning" | "info" | "neutral" } {
  if (run.status !== "completed") return { label: run.status === "queued" ? "в очереди" : "выполняется", tone: "info" };
  switch (run.conclusion) {
    case "success":
      return { label: "успешно", tone: "success" };
    case "failure":
    case "timed_out":
    case "startup_failure":
      return { label: run.conclusion === "failure" ? "провал" : run.conclusion, tone: "danger" };
    case "cancelled":
      return { label: "отменён", tone: "neutral" };
    case "skipped":
    case "neutral":
      return { label: run.conclusion, tone: "neutral" };
    default:
      return { label: run.conclusion ?? "—", tone: "warning" };
  }
}
