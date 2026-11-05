// Typed client of the REST API (/api/v1). The token lives in sessionStorage only:
// it is gone when the tab closes, and the strict CSP keeps third-party scripts out.

export interface Project {
  id: string;
  name: string;
  role: string | null;
  created_at: string;
}

export interface Member {
  username: string;
  role: string;
}

export interface ProjectDetail extends Project {
  members: Member[];
  kinds: string[];
}

export interface RunSummary {
  id: string;
  state: string;
  mode: string;
  goal: string;
  kind: string | null;
  requested_by: string;
  created_at: string;
  finished_at: string | null;
}

export interface Task {
  key: string;
  phase: string;
  status: string;
  attempts: number;
  error: string | null;
}

export interface Approval {
  id: string;
  operation: string;
  status: string;
  reason: string | null;
  decided_by: string | null;
}

export interface RunDetail {
  id: string;
  state: string;
  mode: string;
  goal: string;
  error: string | null;
  kind: string | null;
  project_id: string;
  tasks: Task[];
  approvals: Approval[];
  jobs: { kind: string; state: string; attempts: number; error: string | null }[];
}

export interface ArtifactInfo {
  id: string;
  name: string;
  sha256: string;
  media_type: string;
  size_bytes: number;
}

export interface FindingRow {
  id: string;
  source: string;
  rule_id: string | null;
  severity: string;
  file_path: string | null;
  line: number | null;
  title: string;
  status: string;
}

export interface AuditRow {
  id: string;
  created_at: string;
  actor: string;
  action: string;
  outcome: string;
  run_id: string | null;
  target: string | null;
}

export interface Me {
  username: string;
  is_admin: boolean;
}

export const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
const TOKEN_KEY = "ai-coding-token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit",
  });
  if (response.status === 401) setToken(null);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = (await response.json()) as { error?: { message?: string } };
      message = data.error?.message ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function download(path: string, filename: string): Promise<void> {
  const token = getToken();
  const response = await fetch(`/api/v1${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: "omit",
  });
  if (!response.ok) throw new ApiError(response.status, `HTTP ${response.status}`);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string }>("POST", "/auth/login", { username, password }),
  me: () => request<Me>("GET", "/me"),
  projects: () => request<Project[]>("GET", "/projects"),
  project: (id: string) => request<ProjectDetail>("GET", `/projects/${id}`),
  runs: (projectId: string) => request<RunSummary[]>("GET", `/projects/${projectId}/runs`),
  createRun: (projectId: string, kind: string, params: Record<string, unknown>) =>
    request<RunDetail>("POST", `/projects/${projectId}/runs`, { kind, params }),
  run: (id: string) => request<RunDetail>("GET", `/runs/${id}`),
  decide: (id: string, approve: boolean, note: string) =>
    request<{ state: string }>("POST", `/runs/${id}/approve`, { approve, note }),
  cancel: (id: string) => request<{ state: string }>("POST", `/runs/${id}/cancel`, {}),
  artifacts: (id: string) => request<ArtifactInfo[]>("GET", `/runs/${id}/artifacts`),
  findings: (projectId: string) => request<FindingRow[]>("GET", `/projects/${projectId}/findings`),
  audit: (projectId: string) =>
    request<AuditRow[]>("GET", `/audit-events?project_id=${encodeURIComponent(projectId)}`),
  setMember: (projectId: string, username: string, role: string) =>
    request<ProjectDetail>(
      "PUT",
      `/projects/${projectId}/members/${encodeURIComponent(username)}`,
      { role },
    ),
  removeMember: (projectId: string, username: string) =>
    request<void>("DELETE", `/projects/${projectId}/members/${encodeURIComponent(username)}`),
};
