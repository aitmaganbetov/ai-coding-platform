import { keepPreviousData, QueryClient, useQuery } from "@tanstack/react-query";
import { ApiError, api, readiness } from "../api";

// Server state of the NEXUS views (TanStack Query): cached per key, refreshed while it
// matters (active runs poll; finished data does not), never invented on the client.

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      // A missing endpoint or a refused permission will not change by retrying.
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

const LIVE = 5_000;

export const useProjects = () => useQuery({ queryKey: ["projects"], queryFn: api.projects });

export const useProject = (id: string | null | undefined) =>
  useQuery({ queryKey: ["project", id], queryFn: () => api.project(id as string), enabled: Boolean(id) });

export function useRuns(filter: { state?: string; kind?: string; project_id?: string; limit?: number } = {}) {
  return useQuery({
    queryKey: ["runs", filter],
    queryFn: () => api.allRuns({ limit: 100, ...filter }),
    placeholderData: keepPreviousData,
    refetchInterval: (q) =>
      q.state.data?.some((r) => !["COMPLETED", "FAILED", "CANCELLED"].includes(r.state)) ? LIVE : 30_000,
  });
}

export function useRun(id: string | null | undefined) {
  return useQuery({
    queryKey: ["run", id],
    queryFn: () => api.run(id as string),
    enabled: Boolean(id),
    refetchInterval: (q) =>
      q.state.data && !["COMPLETED", "FAILED", "CANCELLED"].includes(q.state.data.state) ? LIVE : false,
  });
}

export const useFindings = (filter: Parameters<typeof api.allFindings>[0] = {}) =>
  useQuery({ queryKey: ["findings", filter], queryFn: () => api.allFindings({ limit: 1000, ...filter }), placeholderData: keepPreviousData });

export const useEvents = (limit = 40) =>
  useQuery({ queryKey: ["events", limit], queryFn: () => api.events(limit), refetchInterval: 30_000 });

export const useAgents = () => useQuery({ queryKey: ["agents"], queryFn: api.agents, staleTime: 10 * 60_000 });

export const useAgentExecutions = (projectId: string | null | undefined) =>
  useQuery({
    queryKey: ["agent-executions", projectId],
    queryFn: () => api.agentExecutions(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: 15_000,
  });

export const useUsage = (projectId: string | null | undefined, days = 30) =>
  useQuery({ queryKey: ["usage", projectId, days], queryFn: () => api.usage(projectId as string, days), enabled: Boolean(projectId) });

export const useSystem = (enabled: boolean) =>
  useQuery({ queryKey: ["system"], queryFn: api.system, enabled, refetchInterval: 15_000 });

export const useReadiness = () =>
  useQuery({ queryKey: ["readiness"], queryFn: readiness, refetchInterval: 30_000, retry: false });

export const useKinds = () => useQuery({ queryKey: ["kinds"], queryFn: () => api.projects().then(() => null), enabled: false });
