import { useQuery } from "@tanstack/react-query";
import { Bot, Maximize2, X } from "lucide-react";
import { useState } from "react";
import { api, errorText } from "../api";
import { ChatPanel } from "../ChatPanel";
import { useProject, useProjects } from "../data/queries";
import { Button, EmptyState, ErrorState, IconButton, Skeleton } from "../ui";
import { href } from "./routes";
import { useUi } from "./store";

// The AI Copilot panel: the real chat of the user's workspace in the current project
// (Claude Code or Codex in the background, ADR-0037), beside any page.

function CopilotBody({ projectId }: { projectId: string }) {
  const info = useQuery({ queryKey: ["workspace", projectId], queryFn: () => api.workspace(projectId) });
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (info.isLoading) return <Skeleton lines={4} />;
  if (info.error) return <ErrorState error={info.error} onRetry={() => void info.refetch()} />;
  const ws = info.data;
  if (!ws) return null;
  if (ws.terminal === "off")
    return <EmptyState title="Рабочие области выключены">Администратор сервера отключил рабочие области (AI_CODING_SERVER_WORKSPACES).</EmptyState>;
  if (!ws.git)
    return <EmptyState title="Проект без git">Чату нужна рабочая область, а ей — git-репозиторий проекта.</EmptyState>;
  if (!ws.exists) {
    if (!ws.can_edit) return <EmptyState title="Нет доступа к рабочей области">Ваша роль в проекте позволяет только просмотр.</EmptyState>;
    return (
      <EmptyState
        icon={<Bot size={20} />}
        title="Рабочая область ещё не создана"
        action={
          <Button
            variant="primary"
            loading={opening}
            onClick={() => {
              setOpening(true);
              setError(null);
              api.openWorkspace(projectId).then(
                () => void info.refetch().finally(() => setOpening(false)),
                (err: unknown) => {
                  setError(errorText(err));
                  setOpening(false);
                },
              );
            }}
          >
            Создать мою рабочую область
          </Button>
        }
      >
        Своя копия репозитория, где Claude Code или Codex работают по вашей подписке.
        {error && <p className="error">{error}</p>}
      </EmptyState>
    );
  }
  return (
    <ChatPanel
      projectId={projectId}
      onOpenTerminal={(command) => (window.location.hash = href("code", projectId, { terminal: command }))}
      onFilesChanged={() => undefined}
    />
  );
}

export function Copilot() {
  const { projectId, setCopilot, setProject } = useUi();
  const project = useProject(projectId);
  const projects = useProjects();
  return (
    <aside className="copilot" aria-label="AI Copilot">
      <div className="copilot-head">
        <Bot size={16} aria-hidden className="tone-accent" />
        <div className="copilot-title">
          <strong>AI Copilot</strong>
          <small className="truncate">{project.data?.name ?? "проект не выбран"}</small>
        </div>
        {projectId && <IconButton label="Открыть AI Workspace" icon={<Maximize2 size={14} />} onClick={() => (window.location.hash = href("ai", projectId))} />}
        <IconButton label="Скрыть AI Copilot" icon={<X size={14} />} onClick={() => setCopilot(false)} />
      </div>
      <div className="copilot-body">
        {projectId ? (
          <CopilotBody key={projectId} projectId={projectId} />
        ) : (
          <EmptyState title="Выберите проект" icon={<Bot size={20} />}>
            Copilot работает в рабочей области проекта.
            <div className="copilot-projects">
              {(projects.data ?? []).slice(0, 8).map((p) => (
                <Button key={p.id} size="sm" onClick={() => setProject(p.id)}>
                  {p.name}
                </Button>
              ))}
            </div>
          </EmptyState>
        )}
      </div>
    </aside>
  );
}
