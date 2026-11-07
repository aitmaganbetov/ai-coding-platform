import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../api";
import { queryClient } from "./queries";

// The orchestrator's AI provider configured from the web (ADR-0046). The web settings
// override the project's .ai-coding.yml for server runs; the API key is the write-only
// project secret "ai.api_key" (SecretField) — no answer here ever carries it.

export type AIProvider = "openai" | "anthropic" | "ollama" | "vllm";

export interface AIBudgets {
  max_tokens_per_run: number;
  max_cost_usd_per_run: number;
  max_duration_minutes: number;
}

/** The web's own values: a missing field inherits the project's file. */
export interface AISettings {
  enabled?: boolean;
  provider?: AIProvider;
  model?: string;
  base_url?: string;
  routing?: Record<string, string>; // agent → model of the same provider
  fallback?: string[];
  budgets?: Partial<AIBudgets>;
  private?: boolean;
}

export type AIField = keyof AISettings;
export const AI_FIELDS: AIField[] = ["enabled", "provider", "model", "base_url", "routing", "fallback", "budgets", "private"];

/** A resolved configuration: the file's, or the effective one (file + web). */
export interface AIView {
  enabled: boolean;
  provider: AIProvider | "none";
  model: string | null;
  base_url: string | null;
  routing: Record<string, string>;
  fallback: string[];
  budgets: AIBudgets;
  private: boolean;
  network: string;
}

export interface AIState {
  settings: AISettings;
  version: number;
  updated_by: string | null;
  updated_at: string | null;
  file: AIView;
  effective: AIView;
  sources: Record<AIField, "web" | "file">;
  /** Whether a key is available — never the key; `env` is a variable's NAME. */
  key: { set: boolean; source: "web" | "env" | null; env: string | null };
  valid: boolean;
  error: string | null;
  can_edit: boolean;
  secrets_available: boolean;
  agents: string[];
}

export interface AIModels {
  provider: string;
  models: string[];
  reason: string | null;
}

export interface AITestResult {
  ok: boolean;
  provider: string;
  model: string | null;
  latency_ms: number;
  models_offered: number;
  detail: string;
}

const base = (projectId: string) => `/projects/${encodeURIComponent(projectId)}/ai`;

export const aiApi = {
  get: (projectId: string) => apiRequest<AIState>("GET", base(projectId)),
  put: (projectId: string, settings: AISettings, version: number | null) =>
    apiRequest<AIState>("PUT", base(projectId), { settings, version }),
  test: (projectId: string) => apiRequest<AITestResult>("POST", `${base(projectId)}/test`),
  models: (projectId: string) => apiRequest<AIModels>("GET", `${base(projectId)}/models`),
};

export const aiKey = (projectId: string) => ["ai", projectId] as const;
export const aiModelsKey = (projectId: string, provider: string, baseUrl: string | null) =>
  ["ai-models", projectId, provider, baseUrl ?? ""] as const;

export const useAI = (projectId: string | null | undefined) =>
  useQuery({ queryKey: aiKey(projectId ?? ""), queryFn: () => aiApi.get(projectId as string), enabled: Boolean(projectId) });

/** The models the saved provider offers (owners and maintainers: the API calls the provider). */
export const useAIModels = (projectId: string, state: AIState | undefined) =>
  useQuery({
    queryKey: aiModelsKey(projectId, state?.effective.provider ?? "none", state?.effective.base_url ?? null),
    queryFn: () => aiApi.models(projectId),
    enabled: Boolean(state?.can_edit && state.effective.provider !== "none"),
    staleTime: 5 * 60_000,
    retry: false,
  });

/** After a change: the settings, the project's resolved config and the model list. */
export function refreshAI(projectId: string): Promise<unknown> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: aiKey(projectId) }),
    queryClient.invalidateQueries({ queryKey: ["ai-models", projectId] }),
    queryClient.invalidateQueries({ queryKey: ["project", projectId] }),
  ]);
}
