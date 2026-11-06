import { ArrowRight, Code2, FolderGit2, FolderPlus, Sparkles } from "lucide-react";
import { useProjects } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import { Badge, EmptyState, LinkButton, PageHeader, QueryState, relativeTime, Skeleton } from "../ui";

const TEXT = {
  ai: {
    title: "AI Workspace",
    subtitle: "Чат с Claude Code или Codex в вашей рабочей области: режим работы, план, разрешения, проверки оркестратора — по выбранному проекту.",
    icon: <Sparkles size={18} />,
  },
  code: {
    title: "Code Studio",
    subtitle: "Файлы, редактор, git и терминал вашей рабочей области проекта.",
    icon: <Code2 size={18} />,
  },
};

/** Opens the AI Workspace or Code Studio of a project (they work per project). */
export default function Launcher({ target }: { target: "ai" | "code" }) {
  const projects = useProjects();
  const { projectId, setProject } = useUi();
  const text = TEXT[target];
  const current = projects.data?.find((p) => p.id === projectId);
  return (
    <div className="page">
      <PageHeader
        icon={text.icon}
        title={text.title}
        subtitle={text.subtitle}
        actions={
          current && (
            <LinkButton variant="primary" href={href(target, current.id)} icon={<ArrowRight size={14} />}>
              Продолжить в «{current.name}»
            </LinkButton>
          )
        }
      />
      <QueryState
        query={projects}
        skeleton={<Skeleton lines={4} />}
        empty={(list) =>
          list.length === 0 ? (
            <EmptyState
              icon={<FolderGit2 size={20} />}
              title="Нет проектов"
              action={
                <LinkButton variant="primary" href={href("new")} icon={<FolderPlus size={14} />}>
                  Подключить проект
                </LinkButton>
              }
            >
              Подключите папку, Git-репозиторий или каталог на сервере.
            </EmptyState>
          ) : null
        }
      >
        {(list) => (
          <div className="launcher-grid">
            {list.map((p) => (
              <a
                key={p.id}
                className={p.id === projectId ? "launcher-card current" : "launcher-card"}
                href={href(target, p.id)}
                onClick={() => setProject(p.id)}
              >
                <span className="project-avatar" aria-hidden>
                  {p.name.slice(0, 2).toUpperCase()}
                </span>
                <span className="launcher-text">
                  <strong className="truncate">{p.name}</strong>
                  <small>
                    {p.role ?? "администратор"} · создан {relativeTime(p.created_at)}
                  </small>
                </span>
                {p.id === projectId && <Badge tone="accent">текущий</Badge>}
                <ArrowRight size={14} aria-hidden className="launcher-go" />
              </a>
            ))}
          </div>
        )}
      </QueryState>
    </div>
  );
}
