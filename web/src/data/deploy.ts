import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../api";
import { queryClient } from "./queries";

// Deployments (ADR-0048): environments of a project and its deploy history. A deployment
// is a "deploy" run of the orchestrator; production waits for a person's approval.

export type EnvironmentKind = "github_actions" | "webhook";
export type DeploymentState = "queued" | "awaiting_approval" | "running" | "succeeded" | "failed" | "cancelled";

export interface Deployment {
  id: string;
  environment_id: string;
  environment: string | null;
  ref: string;
  commit: string | null;
  state: DeploymentState;
  external_url: string | null;
  detail: string | null;
  requested_by: string;
  run_id: string | null;
  created_at: string | null;
  finished_at: string | null;
}

/** What the editor sends (the server validates it again). */
export interface EnvironmentInput {
  name: string;
  kind: EnvironmentKind;
  production: boolean;
  requires_approval: boolean;
  require_gates: boolean;
  position?: number;
  workflow?: string | null;
  environment_input?: string | null;
  ref_input?: string | null;
  webhook_url?: string | null;
  health_url?: string | null;
  timeout_minutes?: number;
}

export interface Environment extends Required<Omit<EnvironmentInput, "position">> {
  id: string;
  position: number;
  secret_name: string | null; // the webhook's signing secret (deploy.<name>.webhook)
  created_at: string | null;
  updated_at: string | null;
  last_deployment: Deployment | null;
}

export interface Environments {
  environments: Environment[];
  can_manage: boolean;
  can_deploy: boolean;
}

const project = (id: string) => `/projects/${encodeURIComponent(id)}`;

export const deployApi = {
  environments: (projectId: string) => apiRequest<Environments>("GET", `${project(projectId)}/environments`),
  createEnvironment: (projectId: string, env: EnvironmentInput) => apiRequest<Environment>("POST", `${project(projectId)}/environments`, env),
  updateEnvironment: (projectId: string, id: string, env: EnvironmentInput) =>
    apiRequest<Environment>("PUT", `${project(projectId)}/environments/${encodeURIComponent(id)}`, env),
  deleteEnvironment: (projectId: string, id: string) => apiRequest<void>("DELETE", `${project(projectId)}/environments/${encodeURIComponent(id)}`),
  deployments: (projectId: string, limit = 50) => apiRequest<Deployment[]>("GET", `${project(projectId)}/deployments?limit=${limit}`),
  deploy: (projectId: string, environmentId: string, ref: string) =>
    apiRequest<Deployment>("POST", `${project(projectId)}/deployments`, { environment_id: environmentId, ref }),
};

export const ACTIVE_STATES = new Set<DeploymentState>(["queued", "awaiting_approval", "running"]);

export const deployKey = (projectId: string) => ["deploy", projectId] as const;

export const useEnvironments = (projectId: string | null | undefined) =>
  useQuery({
    queryKey: [...deployKey(projectId ?? ""), "environments"],
    queryFn: () => deployApi.environments(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: (q) => (q.state.data?.environments.some((e) => e.last_deployment && ACTIVE_STATES.has(e.last_deployment.state)) ? 5_000 : 30_000),
  });

export const useDeployments = (projectId: string | null | undefined) =>
  useQuery({
    queryKey: [...deployKey(projectId ?? ""), "history"],
    queryFn: () => deployApi.deployments(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: (q) => (q.state.data?.some((d) => ACTIVE_STATES.has(d.state)) ? 5_000 : 30_000),
  });

export function refreshDeploy(projectId: string): void {
  void queryClient.invalidateQueries({ queryKey: deployKey(projectId) });
}

export const STATE_TEXT: Record<DeploymentState, { label: string; tone: "neutral" | "info" | "warning" | "success" | "danger" }> = {
  queued: { label: "в очереди", tone: "neutral" },
  awaiting_approval: { label: "ждёт подтверждения", tone: "warning" },
  running: { label: "выполняется", tone: "info" },
  succeeded: { label: "успешно", tone: "success" },
  failed: { label: "ошибка", tone: "danger" },
  cancelled: { label: "отменён", tone: "neutral" },
};
