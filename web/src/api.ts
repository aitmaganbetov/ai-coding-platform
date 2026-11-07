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
  config?: ProjectConfig | null;
}

/** Non-secret summary of a project's .ai-coding.yml (null when it cannot be read). */
export interface ProjectConfig {
  stacks: string[];
  mode: string;
  execution: { backend: string; network: string };
  ai: { enabled: boolean; provider: string; model: string | null };
  security: { network: string; egress_allowlist: string[] };
  budgets: { max_tokens_per_run: number; max_cost_usd_per_run: number; max_duration_minutes: number };
  quality_gates: Record<string, { enabled: boolean; blocking: boolean; [key: string]: unknown }>;
}

/** A workspace's changes for the AI Changes Review (GET …/workspace/diff, ADR-0040). */
export interface DiffLine {
  kind: "context" | "add" | "del" | "meta";
  text: string;
}

export interface DiffHunk {
  header: string;
  section: string;
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  lines: DiffLine[];
  /** The file's header and this hunk: what `git apply` takes for this hunk alone. */
  patch: string;
}

export interface DiffFile {
  path: string;
  old_path: string | null;
  status: "modified" | "added" | "deleted" | "renamed";
  binary: boolean;
  header: string;
  hunks: DiffHunk[];
  additions: number;
  deletions: number;
}

export interface WorkspaceDiff {
  accepted: DiffFile[];
  pending: DiffFile[];
  new_files: DiffFile[];
  truncated: boolean;
}

export type ReviewAction =
  | "accept"
  | "reject"
  | "unstage"
  | "accept_file"
  | "reject_file"
  | "accept_path"
  | "reject_path"
  | "unstage_path";

/** A developer's machine reporting its local `ai-coding` runs (ADR-0047): summaries only. */
export interface LocalMachine {
  machine: string;
  user: string;
  reports: number;
  project_id: string;
  project: string | null;
  mode: string;
  state: string;
  verdict: "PASS" | "FAIL" | null;
  cli_version: string;
  reported_at: string | null;
}

export interface LocalRunsOverview {
  machines: LocalMachine[];
  reports_24h: number;
  projects: number;
  last_report: string | null;
}

/** A secret of a project: write-only — only its hint (last characters) comes back. */
export interface SecretInfo {
  name: string;
  hint: string;
  updated_by: string;
  updated_at: string;
}

/** A section of a project's settings made in the Web UI (ai, github…), with its version. */
export interface SettingsSection<T> {
  data: T;
  version: number;
  updated_by: string | null;
  updated_at: string | null;
}

/** A run of any project the caller may view (GET /runs). */
export interface GlobalRun extends RunSummary {
  project_id: string;
  project_name: string;
}

export interface Gate {
  gate: string;
  result: string;
  blocking: boolean;
  reason: string | null;
  evidence?: string[];
}

export interface AgentInfo {
  name: string;
  inputs: string;
  outputs: string;
  requires_llm: boolean;
  implemented: boolean;
  sprint: string;
  capabilities: string[];
  allowed_tools: string[];
}

export interface AgentExecution {
  id: string;
  run_id: string;
  task_key: string;
  agent_name: string;
  attempt: number;
  status: string;
  confidence: number | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  models: string[];
}

export interface Usage {
  totals: { calls: number; input_tokens: number; output_tokens: number; cost_usd: number };
  by_model: { provider: string; model: string; calls: number; input_tokens: number; output_tokens: number; cost_usd: number }[];
  daily: { date: string; input_tokens: number; output_tokens: number; cost_usd: number }[];
}

export interface SystemInfo {
  version: string;
  database: string;
  workspaces: string;
  queue: Record<string, number>;
  runs: Record<string, number>;
  /** Workers by heartbeat (idle ones too) and job leases; online: a fresh heartbeat or a live lease. */
  workers: {
    id: string;
    online: boolean;
    active_jobs: number;
    last_seen: string | null;
    host: string | null;
    started: string | null;
    concurrency: number | null;
  }[];
  analysers: Record<string, boolean>;
  time: string;
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
  dependencies?: { task: string; depends_on: string }[];
  gates?: Gate[] | null;
  usage?: { input_tokens: number; output_tokens: number; cost_usd: number };
  /** Per finished step: branch, base, commit, url, iterations, verdict, summary (if any). */
  results?: Record<string, Record<string, string | number>>;
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
  message?: string;
  run_id?: string | null;
  project_id?: string;
  project_name?: string;
  fingerprint?: string;
  evidence?: Record<string, unknown>;
  created_at?: string;
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

/** Opt-in modules the server has switched on (GET /me). Access is still checked per request. */
export interface Features {
  security_center?: boolean;
}

export interface Me {
  username: string;
  is_admin: boolean;
  can_create_projects?: boolean;
  token_id?: string | null;
  features?: Features;
}

// --------------------------------------------- AI Security Center (opt-in) --
// Read-only view of one run (docs/security-center.md). Every string comes from scanners
// and the scanned project: untrusted, rendered as text only.

export type ToolState = "completed" | "skipped" | "error" | "running" | "not_run";
export type CoverageState = "completed" | "incomplete" | "skipped" | "error" | "unsupported";
export type SecurityCategory = "sast" | "sca" | "secrets" | "iac" | "container" | "dast";

export interface CoverageTool {
  tool: string;
  state: ToolState;
  /** A fixed code (timeout, offline_db_missing…); the tool's own error text stays on the server. */
  reason: string | null;
}

export interface CoverageCategory {
  available: boolean;
  state: CoverageState;
  tools: CoverageTool[];
}

export type AgentVerdict = "confirmed" | "false_positive" | "needs_review";

export interface SecurityFinding {
  id: string;
  source: string;
  category: "sast" | "sca" | "secrets" | "iac" | "other";
  rule_id: string | null;
  severity: string;
  title: string;
  file_path: string | null;
  line: number | null;
  fingerprint: string;
  status: string;
  created_at: string | null;
  /** The Security Agent's advisory verdict (null: not assessed). Never changes the gates. */
  agent_verdict?: AgentVerdict | null;
}

/** One assessment of the Security Agent; its code evidence is withheld by the server. */
export interface AgentAssessment {
  verdict: AgentVerdict;
  severity: string | null;
  exploit_scenario: string | null;
  recommendation: string | null;
  cwe: string | null;
}

export interface AgentRisk {
  file: string | null;
  line: number | null;
  severity: string;
  description: string | null;
  recommendation: string | null;
}

export interface SecurityAgentState {
  state: "not_requested" | "awaiting_approval" | "running" | "completed" | "skipped" | "error" | "not_run";
  reason: string | null;
  verdicts?: Record<AgentVerdict, number>;
  triaged?: number;
  total?: number | null;
  models?: string[];
  summary?: string | null;
  additional_risks?: AgentRisk[];
}

export interface SecurityFindingDetail extends SecurityFinding {
  details: {
    package?: string;
    installed_version?: string;
    fixed_version?: string;
    ecosystem?: string;
    aliases?: string[];
    reference?: string;
    resolution?: string;
    cwe?: string[];
  };
  withheld: string[];
  agent?: AgentAssessment | null;
}

export interface SecurityOverview {
  schema_version: string;
  project_id: string;
  run_id: string;
  run: { state: string; finished: boolean; mode: string; kind: string | null; created_at: string; finished_at: string | null };
  coverage: Record<SecurityCategory, CoverageCategory>;
  security_gate: { result: string; blocking: boolean } | null;
  security_agent?: SecurityAgentState;
  launch?: { kind: string; tools: string[] };
  summary: { total: number; by_severity: Record<string, number>; by_source: Record<string, number>; by_status: Record<string, number> };
  findings: SecurityFinding[];
  page: { limit: number; offset: number; total: number; filters: { severity: string | null; source: string | null; status: string | null } };
  capabilities: { dast: string; container: string };
  compliance: { state: string; attestation: boolean };
  ai_fix: { state: string; requires_human_review: boolean };
}

export interface SecurityQuery {
  limit?: number;
  offset?: number;
  severity?: string;
  source?: string;
  status?: string;
}

export interface UserRow {
  id: string;
  username: string;
  display_name: string | null;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
}

export interface NewUser {
  username: string;
  password: string;
  display_name?: string;
  is_admin: boolean;
}

export interface UserPatch {
  display_name?: string;
  is_admin?: boolean;
  is_active?: boolean;
}

export interface SourceDirectory {
  name: string;
  path: string;
  git: boolean;
  used: boolean; // another project's directory is it, contains it or lies in it
  project: string | null; // that project's name, when the caller may know it
}

export interface SourceRoot extends SourceDirectory {
  directories: SourceDirectory[];
}

export interface ProjectSources {
  git: boolean;
  upload_max_bytes: number;
  roots: SourceRoot[];
}

// Exactly one source: a Git URL (the server clones it) or a directory on the server.
// An uploaded folder goes through api.uploadProject instead.
export type NewProject =
  | { name: string; git_url: string; branch?: string; git_token?: string }
  | { name: string; path: string };

export interface TokenRow {
  id: string;
  name: string;
  project_id: string | null;
  max_role: string | null;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked: boolean;
}

export interface NewToken {
  name: string;
  project_id?: string;
  max_role?: string;
  ttl_hours?: number;
}

export const ROLES = ["owner", "maintainer", "developer", "auditor", "agent"];

export interface WorkspaceInfo {
  git: boolean; // the project is a git repository (workspaces need one)
  exists: boolean; // the caller's own workspace was created
  can_edit: boolean; // the caller's role may use a workspace
  terminal: "on" | "off";
  branch_prefix: string; // "ws/<user>/": where "publish" puts the workspace branch
}

export type FileScope = "workspace" | "project";

export interface FileEntry {
  name: string;
  type: "dir" | "file" | "link" | "other";
  size: number;
}

export interface FileContent {
  path: string;
  size: number;
  content: string | null;
  binary: boolean;
  too_large: boolean;
  version: string | null;
}

export interface GitStatus {
  branch: string | null;
  files: { status: string; path: string }[];
}

export interface GitResult {
  ok: boolean;
  output: string;
  branch: string | null;
}

const fileQuery = (path: string, scope?: FileScope) =>
  `path=${encodeURIComponent(path)}${scope ? `&scope=${scope}` : ""}`;

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

/** The typed request of this client, for the clients of other sections (src/data/*). */
export function apiRequest<T>(method: string, path: string, body?: unknown): Promise<T> {
  return request<T>(method, path, body);
}

function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (body === undefined) return send<T>(method, path);
  return send<T>(method, path, JSON.stringify(body), "application/json");
}

async function send<T>(
  method: string,
  path: string,
  body?: BodyInit,
  contentType?: string,
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (contentType) headers["Content-Type"] = contentType;
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    body,
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

/** A run artifact's content as text (implementation.json, change.diff…). */
export async function artifactText(runId: string, artifactId: string): Promise<string> {
  const token = getToken();
  const response = await fetch(`/api/v1/runs/${runId}/artifacts/${artifactId}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: "omit",
  });
  if (!response.ok) throw new ApiError(response.status, `HTTP ${response.status}`);
  return response.text();
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function shortDate(iso: string | null): string {
  return iso ? iso.slice(0, 19).replace("T", " ") : "—";
}

const user = (username: string) => `/users/${encodeURIComponent(username)}`;

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string }>("POST", "/auth/login", { username, password }),
  me: () => request<Me>("GET", "/me"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("PUT", "/me/password", { current_password, new_password }),
  tokens: () => request<TokenRow[]>("GET", "/tokens"),
  issueToken: (body: NewToken) =>
    request<{ token: string; token_id: string }>("POST", "/tokens", body),
  revokeToken: (id: string) => request<void>("DELETE", `/tokens/${id}`),
  users: () => request<UserRow[]>("GET", "/users"),
  createUser: (body: NewUser) => request<{ id: string }>("POST", "/users", body),
  updateUser: (username: string, patch: UserPatch) =>
    request<UserRow>("PATCH", user(username), patch),
  resetPassword: (username: string, password: string) =>
    request<void>("PUT", `${user(username)}/password`, { password }),
  projects: () => request<Project[]>("GET", "/projects"),
  projectSources: () => request<ProjectSources>("GET", "/project-sources"),
  createProject: (body: NewProject) => request<ProjectDetail>("POST", "/projects", body),
  uploadProject: (name: string, archive: Blob) =>
    send<ProjectDetail>(
      "POST",
      `/projects/upload?name=${encodeURIComponent(name)}`,
      archive,
      "application/x-tar",
    ),
  deleteProject: (id: string) => request<void>("DELETE", `/projects/${id}`),
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
  workspace: (projectId: string) => request<WorkspaceInfo>("GET", `/projects/${projectId}/workspace`),
  openWorkspace: (projectId: string) =>
    request<WorkspaceInfo>("POST", `/projects/${projectId}/workspace`, {}),
  files: (projectId: string, path: string, scope: FileScope) =>
    request<FileEntry[]>("GET", `/projects/${projectId}/files?${fileQuery(path, scope)}`),
  file: (projectId: string, path: string, scope: FileScope) =>
    request<FileContent>("GET", `/projects/${projectId}/file?${fileQuery(path, scope)}`),
  saveFile: (projectId: string, path: string, content: string, version: string | null, create = false) =>
    request<{ path: string; version: string }>("PUT", `/projects/${projectId}/file?${fileQuery(path)}`, {
      content,
      version,
      create,
    }),
  deleteFile: (projectId: string, path: string) =>
    request<void>("DELETE", `/projects/${projectId}/file?${fileQuery(path)}`),
  restartWorkspace: (projectId: string) =>
    request<{ status: string }>("POST", `/projects/${projectId}/workspace/restart`, {}),
  gitStatus: (projectId: string) =>
    request<GitStatus>("GET", `/projects/${projectId}/workspace/git`),
  commit: (projectId: string, message: string, stagedOnly = false) =>
    request<GitResult>("POST", `/projects/${projectId}/workspace/commit`, stagedOnly ? { message, staged_only: true } : { message }),
  workspaceDiff: (projectId: string) => request<WorkspaceDiff>("GET", `/projects/${projectId}/workspace/diff`),
  review: (projectId: string, body: { action: ReviewAction; patch?: string; path?: string }) =>
    request<{ ok: boolean; output: string }>("POST", `/projects/${projectId}/workspace/review`, body),
  pullProject: (projectId: string) =>
    request<GitResult>("POST", `/projects/${projectId}/workspace/pull`, {}),
  publish: (projectId: string) =>
    request<{ branch: string; commit: string }>("POST", `/projects/${projectId}/workspace/publish`, {}),
  // NEXUS views: data across projects (only those the caller may see) and insights.
  allRuns: (filter: { limit?: number; state?: string; kind?: string; project_id?: string } = {}) =>
    request<GlobalRun[]>("GET", `/runs${query(filter)}`),
  // current: only each project's latest audit (every audit records its findings anew).
  allFindings: ({
    current,
    ...filter
  }: { limit?: number; severity?: string; status?: string; source?: string; project_id?: string; run_id?: string; current?: boolean } = {}) =>
    request<FindingRow[]>("GET", `/findings${query({ ...filter, current: current ? "true" : undefined })}`),
  events: (limit = 50) => request<AuditRow[]>("GET", `/audit-events${query({ limit })}`),
  agents: () => request<AgentInfo[]>("GET", "/agents"),
  agentExecutions: (projectId: string, limit = 100) =>
    request<AgentExecution[]>("GET", `/projects/${projectId}/agent-executions${query({ limit })}`),
  usage: (projectId: string, days = 30) => request<Usage>("GET", `/projects/${projectId}/usage${query({ days })}`),
  system: () => request<SystemInfo>("GET", "/system"),
  localRunsOverview: () => request<LocalRunsOverview>("GET", "/local-runs/overview"),
  secrets: (projectId: string) =>
    request<{ available: boolean; secrets: SecretInfo[] }>("GET", `/projects/${projectId}/secrets`),
  setSecret: (projectId: string, name: string, value: string) =>
    request<SecretInfo>("PUT", `/projects/${projectId}/secrets/${encodeURIComponent(name)}`, { value }),
  deleteSecret: (projectId: string, name: string) =>
    request<void>("DELETE", `/projects/${projectId}/secrets/${encodeURIComponent(name)}`),
  // AI Security Center: only mounted when the server enables it (404 otherwise).
  securityCenter: (projectId: string, runId: string, filter: SecurityQuery = {}) =>
    request<SecurityOverview>("GET", `/projects/${enc(projectId)}/security-center/runs/${enc(runId)}${query({ ...filter })}`),
  securityFinding: (projectId: string, runId: string, findingId: string) =>
    request<SecurityFindingDetail>("GET", `/projects/${enc(projectId)}/security-center/runs/${enc(runId)}/findings/${enc(findingId)}`),
};

const enc = encodeURIComponent;

/** Readiness of the server and its database (outside /api/v1, no token). */
export async function readiness(): Promise<boolean> {
  try {
    return (await fetch("/readyz", { credentials: "omit" })).ok;
  } catch {
    return false;
  }
}

function query(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter((e): e is [string, string | number] => e[1] !== undefined && e[1] !== "");
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()}` : "";
}

/** WebSocket URL of the caller's terminal in a project's workspace. */
export function terminalUrl(projectId: string): string {
  return workspaceSocket(projectId, "terminal");
}

/** WebSocket URL of the caller's AI chat (Claude Code, Codex) in a project's workspace. */
export function chatUrl(projectId: string): string {
  return workspaceSocket(projectId, "chat");
}

function workspaceSocket(projectId: string, kind: "terminal" | "chat"): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/api/v1/projects/${projectId}/workspace/${kind}`;
}
