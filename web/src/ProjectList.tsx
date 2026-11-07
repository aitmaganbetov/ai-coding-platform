import { useQueries } from "@tanstack/react-query";
import { Code2, FolderGit2, FolderPlus, LayoutGrid, List, ShieldCheck, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { api, errorText, shortDate, type GlobalRun, type Me, type Project } from "./api";
import { useFindings, useProjects, useRuns } from "./data/queries";
import { NewProjectForm } from "./NewProject";
import { href } from "./shell/routes";
import { useUi } from "./shell/store";
import {
  Badge,
  Button,
  type Column,
  DataTable,
  EmptyState,
  ErrorState,
  IconButton,
  PageHeader,
  relativeTime,
  SearchInput,
  Segmented,
  Select,
  Skeleton,
  StateBadge,
  toast,
} from "./ui";
import "./pages/projects.css";

// Project Explorer: every project the user may see, with what the platform knows about it
// (stack from its configuration, last run, open findings). Enrichments are optional: when
// one of them cannot be loaded the list still works.

const ROLE_FILTERS = [
  { value: "", label: "Все роли" },
  { value: "owner", label: "owner" },
  { value: "maintainer", label: "maintainer" },
  { value: "developer", label: "developer" },
  { value: "auditor", label: "auditor" },
  { value: "none", label: "без доступа" },
];

interface Row {
  project: Project;
  stacks: string[] | null;
  lastRun: GlobalRun | undefined;
  openFindings: number | null;
}

function startAudit(project: Project) {
  api.createRun(project.id, "audit", {}).then(
    (run) => {
      toast({ tone: "success", title: "Аудит запущен", text: project.name });
      window.location.hash = href("run", run.id);
    },
    (err: unknown) => toast({ tone: "danger", title: "Аудит не запущен", text: errorText(err) }),
  );
}

function QuickActions({ project }: { project: Project }) {
  const setProject = useUi((s) => s.setProject);
  if (!project.role) return null;
  const go = (target: string) => () => {
    setProject(project.id);
    window.location.hash = target;
  };
  return (
    <div className="project-actions">
      <IconButton label={`AI Workspace «${project.name}»`} icon={<Sparkles size={14} />} onClick={go(href("ai", project.id))} />
      <IconButton label={`Code Studio «${project.name}»`} icon={<Code2 size={14} />} onClick={go(href("code", project.id))} />
      <IconButton label={`Аудит «${project.name}»`} icon={<ShieldCheck size={14} />} onClick={() => startAudit(project)} />
    </div>
  );
}

function Role({ role }: { role: string | null }) {
  return role ? (
    <Badge tone={role === "owner" ? "accent" : "neutral"}>{role}</Badge>
  ) : (
    <span className="badge tone-neutral" title="Добавить участников может владелец проекта">
      нет доступа
    </span>
  );
}

function Stacks({ stacks }: { stacks: string[] | null }) {
  if (stacks === null) return <span className="text-muted">—</span>;
  if (stacks.length === 0) return <span className="text-muted">не определён</span>;
  return (
    <span className="project-stacks">
      {stacks.map((s) => (
        <span key={s} className="stack-chip">
          {s}
        </span>
      ))}
    </span>
  );
}

function ProjectCard({ row }: { row: Row }) {
  const { project, lastRun } = row;
  const title = project.role ? (
    <a href={href("project", project.id)} className="project-name truncate">
      {project.name}
    </a>
  ) : (
    <span className="project-name truncate">{project.name}</span>
  );
  return (
    <article className="project-card">
      <div className="project-card-head">
        <span className="project-avatar" aria-hidden>
          {project.name.slice(0, 2).toUpperCase()}
        </span>
        <div className="project-card-title">
          {title}
          <small>создан {relativeTime(project.created_at)}</small>
        </div>
        <Role role={project.role} />
      </div>
      <Stacks stacks={row.stacks} />
      <dl className="project-facts">
        <div>
          <dt>Последний запуск</dt>
          <dd>
            {lastRun ? (
              <a href={href("run", lastRun.id)} className="row">
                <StateBadge state={lastRun.state} />
                <span className="text-muted">{relativeTime(lastRun.created_at)}</span>
              </a>
            ) : (
              <span className="text-muted">—</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Открытые находки</dt>
          <dd>
            {row.openFindings === null ? (
              <span className="text-muted">—</span>
            ) : (
              <a href={href("audit", undefined, { project: project.id })} className={row.openFindings > 0 ? "tone-warning" : "text-muted"}>
                {row.openFindings}
              </a>
            )}
          </dd>
        </div>
      </dl>
      <div className="project-card-foot">
        {project.role && (
          <a href={href("project", project.id)} className="tile-link">
            Обзор →
          </a>
        )}
        <span className="grow" />
        <QuickActions project={project} />
      </div>
    </article>
  );
}

export function ProjectList({ me }: { me: Me }) {
  const projects = useProjects();
  const runs = useRuns();
  const findings = useFindings({ current: true }); // each project's latest audit
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const [view, setView] = useState<"grid" | "list">("grid");
  const canCreate = me.can_create_projects ?? me.is_admin;
  const list = projects.data ?? [];

  // The stack comes from each project's configuration (only for projects the user may open).
  const details = useQueries({
    queries: list.map((p) => ({
      queryKey: ["project", p.id],
      queryFn: () => api.project(p.id),
      enabled: Boolean(p.role),
      staleTime: 60_000,
    })),
  });

  const rows: Row[] = useMemo(
    () =>
      list.map((project, i) => {
        const config = details[i]?.data?.config;
        return {
          project,
          stacks: config ? config.stacks : null,
          lastRun: runs.data?.find((r) => r.project_id === project.id),
          openFindings: findings.data ? findings.data.filter((f) => f.project_id === project.id && f.status === "open").length : null,
        };
      }),
    [list, details, runs.data, findings.data],
  );

  const words = search.trim().toLowerCase();
  const shown = rows.filter(
    (r) =>
      (!words || r.project.name.toLowerCase().includes(words) || (r.stacks ?? []).some((s) => s.includes(words))) &&
      (!role || (role === "none" ? !r.project.role : r.project.role === role)),
  );

  const columns: Column<Row>[] = [
    {
      id: "name",
      header: "Проект",
      sort: (r) => r.project.name.toLowerCase(),
      cell: (r) =>
        r.project.role ? (
          <a href={href("project", r.project.id)} className="cell-main">
            {r.project.name}
          </a>
        ) : (
          <span className="cell-main">{r.project.name}</span>
        ),
    },
    { id: "role", header: "Ваша роль", sort: (r) => r.project.role ?? "~", cell: (r) => <Role role={r.project.role} /> },
    { id: "stack", header: "Стек", cell: (r) => <Stacks stacks={r.stacks} /> },
    {
      id: "run",
      header: "Последний запуск",
      sort: (r) => r.lastRun?.created_at ?? "",
      cell: (r) =>
        r.lastRun ? (
          <a href={href("run", r.lastRun.id)} className="row">
            <StateBadge state={r.lastRun.state} />
            <span className="text-muted">{relativeTime(r.lastRun.created_at)}</span>
          </a>
        ) : (
          <span className="text-muted">—</span>
        ),
    },
    { id: "findings", header: "Находки", sort: (r) => r.openFindings ?? -1, cell: (r) => (r.openFindings === null ? "—" : r.openFindings) },
    { id: "created", header: "Создан", sort: (r) => r.project.created_at, cell: (r) => shortDate(r.project.created_at) },
    { id: "actions", header: <span className="sr-only">Действия</span>, cell: (r) => <QuickActions project={r.project} /> },
  ];

  return (
    <div className="page">
      <PageHeader
        icon={<FolderGit2 size={18} />}
        title="Проекты"
        subtitle="Ваши проекты: стек, последние запуски оркестратора, открытые находки и быстрые действия."
        actions={
          canCreate &&
          !creating && (
            <Button variant="primary" icon={<FolderPlus size={14} />} onClick={() => setCreating(true)}>
              Создать или подключить проект
            </Button>
          )
        }
      />
      {creating && <NewProjectForm onCancel={() => setCreating(false)} />}
      {projects.isLoading && <Skeleton lines={4} />}
      {projects.error && <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />}
      {projects.data && list.length === 0 && (
        <EmptyState
          icon={<FolderGit2 size={20} />}
          title="Проектов нет"
          action={
            canCreate && (
              <Button variant="primary" icon={<FolderPlus size={14} />} onClick={() => (window.location.hash = href("new"))}>
                Подключить проект с проверкой
              </Button>
            )
          }
        >
          {canCreate
            ? "Проектов пока нет. Создайте первый: загрузите папку с компьютера или укажите Git-репозиторий."
            : "Нет проектов, доступных вам. Попросите владельца проекта добавить вас в участники."}
        </EmptyState>
      )}
      {list.length > 0 && (
        <>
          <div className="toolbar">
            <SearchInput label="Поиск проектов" value={search} onChange={setSearch} placeholder="Имя или стек…" />
            <Select label="Роль" hideLabel value={role} onChange={setRole} options={ROLE_FILTERS} />
            <span className="grow" />
            <span className="text-muted toolbar-count">
              {shown.length} из {list.length}
            </span>
            <Segmented
              label="Вид списка"
              value={view}
              onChange={setView}
              options={[
                { value: "grid", label: <LayoutGrid size={14} aria-label="Сетка" /> },
                { value: "list", label: <List size={14} aria-label="Список" /> },
              ]}
            />
          </div>
          {shown.length === 0 ? (
            <EmptyState title="Ничего не найдено">Измените поиск или фильтр по роли.</EmptyState>
          ) : view === "grid" ? (
            <div className="project-grid">
              {shown.map((r) => (
                <ProjectCard key={r.project.id} row={r} />
              ))}
            </div>
          ) : (
            <DataTable label="Проекты" rows={shown} columns={columns} rowKey={(r) => r.project.id} initialSort={{ id: "name" }} />
          )}
        </>
      )}
    </div>
  );
}
