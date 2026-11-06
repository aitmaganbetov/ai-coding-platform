import { useQuery } from "@tanstack/react-query";
import {
  Code2,
  FolderGit2,
  Gauge,
  ListChecks,
  Lock,
  Play,
  ScrollText,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Users as UsersIcon,
} from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import {
  ROLES,
  api,
  download,
  errorText,
  type AuditRow,
  type FindingRow,
  type Me,
  type ProjectConfig,
  type ProjectDetail,
  type RunSummary,
} from "./api";
import { queryClient, useProject } from "./data/queries";
import { href } from "./shell/routes";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  LinkButton,
  PageHeader,
  relativeTime,
  SEVERITIES,
  SeverityBadge,
  Skeleton,
  StateBadge,
  Tabs,
  toast,
} from "./ui";
import "./pages/projects.css";

const CAN_RUN = new Set(["owner", "maintainer", "developer"]);
const CAN_AUDIT = new Set(["owner", "maintainer", "auditor"]);
const CAN_VIEW_FILES = new Set(["owner", "maintainer", "developer", "auditor"]);

type Tab = "overview" | "runs" | "findings" | "members" | "journal" | "danger";

export function ProjectPage({ id, me }: { id: string; me: Me }) {
  const project = useProject(id);
  const role = project.data?.role ?? "";
  const runs = useQuery({ queryKey: ["project-runs", id], queryFn: () => api.runs(id), enabled: project.isSuccess });
  const findings = useQuery({ queryKey: ["project-findings", id], queryFn: () => api.findings(id), enabled: project.isSuccess });
  const audit = useQuery({ queryKey: ["project-audit", id], queryFn: () => api.audit(id), enabled: CAN_AUDIT.has(role) });
  const [tab, setTab] = useState<Tab>("overview");
  const [auditing, setAuditing] = useState(false);

  const reload = () => {
    void queryClient.invalidateQueries({ queryKey: ["project", id] });
    void queryClient.invalidateQueries({ queryKey: ["project-runs", id] });
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
  };

  if (project.isLoading) return <Skeleton lines={6} />;
  if (project.error) return <ErrorState error={project.error} onRetry={() => void project.refetch()} />;
  const p = project.data;
  if (!p) return null;
  const runList = runs.data ?? [];
  const findingList = findings.data ?? [];
  const open = findingList.filter((f) => f.status === "open");

  function startAudit() {
    setAuditing(true);
    api.createRun(id, "audit", {}).then(
      (run) => {
        reload();
        toast({ tone: "success", title: "Аудит запущен", text: p?.name });
        window.location.hash = href("run", run.id);
      },
      (err: unknown) => {
        toast({ tone: "danger", title: "Аудит не запущен", text: errorText(err) });
        setAuditing(false);
      },
    );
  }

  const tabs = [
    { id: "overview", label: "Обзор", icon: <Gauge size={14} /> },
    { id: "runs", label: "Задачи", icon: <ListChecks size={14} />, count: runList.length },
    { id: "findings", label: "Находки", icon: <ShieldCheck size={14} />, count: open.length },
    { id: "members", label: "Участники", icon: <UsersIcon size={14} />, count: p.members.length },
    ...(audit.data ? [{ id: "journal", label: "Журнал", icon: <ScrollText size={14} /> }] : []),
    ...(role === "owner" ? [{ id: "danger", label: "Опасная зона", icon: <ShieldAlert size={14} /> }] : []),
  ];

  return (
    <div className="page">
      <PageHeader
        icon={<FolderGit2 size={18} />}
        title={
          <span className="row">
            {p.name} <Badge tone={role === "owner" ? "accent" : "neutral"}>{role || "администратор"}</Badge>
          </span>
        }
        subtitle={`Создан ${relativeTime(p.created_at)} · участников: ${p.members.length}`}
        actions={
          <>
            {CAN_VIEW_FILES.has(role) && (
              <>
                <LinkButton variant="primary" href={href("ai", id)} icon={<Sparkles size={14} />}>
                  AI Workspace
                </LinkButton>
                <LinkButton href={href("code", id)} icon={<Code2 size={14} />}>
                  Код и терминал
                </LinkButton>
              </>
            )}
            {(CAN_RUN.has(role) || role === "auditor") && (
              <Button icon={<ShieldCheck size={14} />} loading={auditing} onClick={startAudit}>
                Аудит
              </Button>
            )}
          </>
        }
      />
      <Tabs label="Разделы проекта" items={tabs} value={tab} onChange={(t) => setTab(t as Tab)} />
      {tab === "overview" && <Overview project={p} runs={runList} findings={findingList} onTab={setTab} />}
      {tab === "runs" && (
        <div className="stack">
          {CAN_RUN.has(role) && <StartRun project={p} onStarted={reload} />}
          {runs.error ? <ErrorState error={runs.error} onRetry={() => void runs.refetch()} /> : <RunTable runs={runList} />}
        </div>
      )}
      {tab === "findings" && (findings.error ? <ErrorState error={findings.error} /> : <FindingTable findings={findingList} />)}
      {tab === "members" && <Members project={p} me={me} onChange={reload} />}
      {tab === "journal" && audit.data && <AuditTable rows={audit.data} />}
      {tab === "danger" && role === "owner" && <Management project={p} />}
    </div>
  );
}

// ---------------------------------------------------------------- overview --
const NETWORK: Record<string, string> = {
  "local-only": "Private Mode: внешние LLM и сеть песочниц запрещены",
  allowlist: "Только разрешённые хосты",
  open: "Сеть открыта",
};

function ConfigSummary({ config }: { config: ProjectConfig | null | undefined }) {
  if (config === undefined) return <p className="text-muted">Сервер не сообщает конфигурацию проекта.</p>;
  if (config === null)
    return (
      <p className="error">
        Конфигурация проекта не читается: проверьте <code>.ai-coding.yml</code> (<code>ai-coding config validate</code>).
      </p>
    );
  return (
    <dl className="kv">
      <dt>Стек</dt>
      <dd>
        {config.stacks.length ? (
          <span className="project-stacks">
            {config.stacks.map((s) => (
              <span key={s} className="stack-chip">
                {s}
              </span>
            ))}
          </span>
        ) : (
          "определяется автоматически"
        )}
      </dd>
      <dt>Режим</dt>
      <dd>
        <code>{config.mode}</code>
      </dd>
      <dt>Исполнение</dt>
      <dd>
        {config.execution.backend} · сеть песочницы <code>{config.execution.network}</code>
      </dd>
      <dt>ИИ оркестратора</dt>
      <dd>
        {config.ai.enabled ? (
          <Badge tone="success" dot>
            {config.ai.provider}
            {config.ai.model ? ` · ${config.ai.model}` : ""}
          </Badge>
        ) : (
          <Badge tone="warning" dot>
            выключен
          </Badge>
        )}
      </dd>
      <dt>Сеть и приватность</dt>
      <dd>
        <span className="row">
          {config.security.network === "local-only" && <Lock size={13} aria-hidden />}
          {NETWORK[config.security.network] ?? config.security.network}
        </span>
        {config.security.egress_allowlist.length > 0 && (
          <span className="cell-sub">{config.security.egress_allowlist.join(", ")}</span>
        )}
      </dd>
      <dt>Бюджет запуска</dt>
      <dd>
        {config.budgets.max_tokens_per_run.toLocaleString("ru")} токенов · ${config.budgets.max_cost_usd_per_run} ·{" "}
        {config.budgets.max_duration_minutes} мин
      </dd>
    </dl>
  );
}

function GatesSummary({ config }: { config: ProjectConfig | null | undefined }) {
  const gates = config ? Object.entries(config.quality_gates) : [];
  if (gates.length === 0) return <p className="text-muted">Нет данных о Quality Gates.</p>;
  return (
    <div className="scroll-x">
      <table className="ws-table gates-table">
        <thead>
          <tr>
            <th>Gate</th>
            <th>Включён</th>
            <th>Блокирует</th>
            <th>Пороги</th>
          </tr>
        </thead>
        <tbody>
          {gates.map(([gate, value]) => {
            const thresholds = Object.entries(value).filter(([k]) => k !== "enabled" && k !== "blocking");
            return (
              <tr key={gate}>
                <td>
                  <code>{gate}</code>
                </td>
                <td>{value.enabled ? "да" : "нет"}</td>
                <td>{value.blocking ? "да" : "нет"}</td>
                <td className="ws-reason">{thresholds.length ? thresholds.map(([k, v]) => `${k}: ${String(v)}`).join(", ") : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Overview({
  project,
  runs,
  findings,
  onTab,
}: {
  project: ProjectDetail;
  runs: RunSummary[];
  findings: FindingRow[];
  onTab: (tab: Tab) => void;
}) {
  const open = findings.filter((f) => f.status === "open");
  return (
    <div className="overview-grid">
      <Card title="Конфигурация" icon={<Gauge size={15} />} className="span-config">
        <ConfigSummary config={project.config} />
      </Card>
      <Card title="Quality Gates" icon={<ShieldCheck size={15} />}>
        <GatesSummary config={project.config} />
      </Card>
      <Card
        title="Последние запуски"
        icon={<Play size={15} />}
        actions={
          <button type="button" className="link-btn tile-link" onClick={() => onTab("runs")}>
            все
          </button>
        }
      >
        {runs.length === 0 ? (
          <p className="text-muted">Запусков оркестратора пока не было.</p>
        ) : (
          <ul className="tile-list">
            {runs.slice(0, 5).map((r) => (
              <li key={r.id}>
                <a href={href("run", r.id)} className="tile-row">
                  <StateBadge state={r.state} />
                  <span className="tile-row-main truncate">{r.goal || r.kind}</span>
                  <span className="tile-row-meta truncate">{r.requested_by}</span>
                  <span className="tile-row-time">{relativeTime(r.created_at)}</span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card
        title="Открытые находки"
        icon={<ShieldAlert size={15} />}
        actions={
          <button type="button" className="link-btn tile-link" onClick={() => onTab("findings")}>
            все
          </button>
        }
      >
        {open.length === 0 ? (
          <p className="text-muted">Открытых находок нет{findings.length ? "" : " (аудитов ещё не было)"}.</p>
        ) : (
          <div className="sev-legend">
            {SEVERITIES.map((s) => (
              <span key={s} className="sev-item">
                <SeverityBadge severity={s} />
                <span className="mono">{open.filter((f) => f.severity.toLowerCase() === s).length}</span>
              </span>
            ))}
          </div>
        )}
      </Card>
      <Card title="Участники" icon={<UsersIcon size={15} />}>
        <ul className="member-chips">
          {project.members.map((m) => (
            <li key={m.username}>
              <span className="member-avatar" aria-hidden>
                {m.username.slice(0, 2).toUpperCase()}
              </span>
              {m.username} <span className="text-muted">{m.role}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

// ----------------------------------------------------------------- danger --
function Management({ project }: { project: ProjectDetail }) {
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function exportData() {
    setError(null);
    try {
      await download(`/projects/${project.id}/export`, `${project.name}-export.zip`);
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function remove(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.deleteProject(project.id);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      window.location.hash = "#/projects";
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <Card title="Управление проектом" icon={<ShieldAlert size={15} />} className="danger-card">
      <div className="actions">
        <Button onClick={() => void exportData()}>Экспорт данных проекта</Button>
        {!confirming && (
          <Button variant="danger" onClick={() => setConfirming(true)}>
            Удалить проект…
          </Button>
        )}
      </div>
      {confirming && (
        <form className="danger-form form" onSubmit={remove} aria-label="Удаление проекта">
          <p>
            Будут удалены прогоны, находки, артефакты и участники проекта; журнал аудита сохраняется. Клон репозитория, сделанный
            сервером, удаляется; подключённый каталог на сервере остаётся на месте. Активные прогоны нужно сначала завершить или
            отменить.
          </p>
          <label>
            Для подтверждения введите имя проекта «{project.name}»
            <input name="confirm" value={typed} onChange={(e) => setTyped(e.target.value)} />
          </label>
          <div className="actions">
            <Button type="submit" variant="danger" disabled={busy || typed !== project.name}>
              Удалить навсегда
            </Button>
            <Button
              onClick={() => {
                setConfirming(false);
                setTyped("");
              }}
            >
              Отмена
            </Button>
          </div>
        </form>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------- runs --
function StartRun({ project, onStarted }: { project: ProjectDetail; onStarted: () => void }) {
  const [kind, setKind] = useState(project.kinds.includes("audit") ? "audit" : project.kinds[0] ?? "");
  const [params, setParams] = useState("{}");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(params) as Record<string, unknown>;
    } catch {
      setError("Параметры должны быть JSON-объектом");
      return;
    }
    try {
      const run = await api.createRun(project.id, kind, parsed);
      onStarted();
      window.location.hash = `#/runs/${run.id}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <form className="panel start-run" onSubmit={submit} aria-label="Запуск оркестратора">
      <label>
        Тип прогона
        <select name="kind" value={kind} onChange={(e) => setKind(e.target.value)}>
          {project.kinds.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </label>
      <label className="grow">
        Параметры (JSON)
        <input name="params" value={params} onChange={(e) => setParams(e.target.value)} className="mono" />
      </label>
      <Button type="submit" variant="primary" icon={<Play size={14} />}>
        Запустить
      </Button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

export function RunTable({ runs }: { runs: RunSummary[] }) {
  if (runs.length === 0) return <EmptyState icon={<ListChecks size={20} />} title="Прогонов нет" />;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Прогон</th>
            <th>Тип</th>
            <th>Состояние</th>
            <th>Запустил</th>
            <th>Создан</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td>
                <a href={`#/runs/${r.id}`} className="mono">
                  {r.id.slice(-12)}
                </a>
                {r.goal && <span className="cell-sub truncate">{r.goal}</span>}
              </td>
              <td>{r.kind ?? r.mode}</td>
              <td>
                <StateBadge state={r.state} />
              </td>
              <td>{r.requested_by}</td>
              <td title={r.created_at}>{relativeTime(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FindingTable({ findings }: { findings: FindingRow[] }) {
  if (findings.length === 0)
    return (
      <EmptyState icon={<ShieldCheck size={20} />} title="Находок нет">
        Находки появляются после аудита: анализаторы проверяют проект в изолированной песочнице.
      </EmptyState>
    );
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Severity</th>
            <th>Инструмент</th>
            <th>Правило</th>
            <th>Файл</th>
            <th>Описание</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((f) => (
            <tr key={f.id}>
              <td>
                <SeverityBadge severity={f.severity} />
              </td>
              <td>{f.source}</td>
              <td>
                <code>{f.rule_id ?? "—"}</code>
              </td>
              <td className="path">
                {f.file_path ?? "—"}
                {f.line ? `:${f.line}` : ""}
              </td>
              <td>{f.title}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- members --
function Members({ project, me, onChange }: { project: ProjectDetail; me: Me; onChange: () => void }) {
  const [username, setUsername] = useState("");
  const [role, setRole] = useState("developer");
  const [known, setKnown] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const owner = project.role === "owner";

  // Administrators may list users: offer them as suggestions (others type the name).
  useEffect(() => {
    if (!owner || !me.is_admin) return;
    api.users().then(
      (users) => setKnown(users.filter((u) => u.is_active).map((u) => u.username)),
      () => setKnown([]),
    );
  }, [owner, me.is_admin]);

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      onChange();
    } catch (err) {
      setError(errorText(err));
    }
  }

  const members = new Set(project.members.map((m) => m.username));
  return (
    <div className="stack">
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Пользователь</th>
              <th>Роль</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {project.members.map((m) => (
              <tr key={m.username}>
                <td className="cell-main">{m.username}</td>
                <td>
                  {owner ? (
                    <select
                      aria-label={`Роль ${m.username}`}
                      value={m.role}
                      onChange={(e) => void act(() => api.setMember(project.id, m.username, e.target.value))}
                    >
                      {ROLES.map((r) => (
                        <option key={r}>{r}</option>
                      ))}
                    </select>
                  ) : (
                    m.role
                  )}
                </td>
                <td className="cell-actions">
                  {owner && (
                    <Button size="sm" variant="ghost" onClick={() => void act(() => api.removeMember(project.id, m.username))}>
                      Удалить
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {owner && (
        <form
          className="panel start-run"
          aria-label="Добавить участника"
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              await api.setMember(project.id, username.trim(), role);
              setUsername("");
            });
          }}
        >
          <label className="grow">
            Пользователь
            <input
              name="member"
              list="known-users"
              placeholder="имя пользователя"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <datalist id="known-users">
            {known
              .filter((u) => !members.has(u))
              .map((u) => (
                <option key={u} value={u} />
              ))}
          </datalist>
          <label>
            Роль
            <select name="role" value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLES.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
          <Button type="submit" variant="primary">
            Добавить участника
          </Button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function AuditTable({ rows }: { rows: AuditRow[] }) {
  if (rows.length === 0) return <EmptyState icon={<ScrollText size={20} />} title="Событий нет" />;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Время</th>
            <th>Кто</th>
            <th>Действие</th>
            <th>Результат</th>
            <th>Объект</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id}>
              <td title={e.created_at}>{relativeTime(e.created_at)}</td>
              <td>{e.actor}</td>
              <td>
                <code>{e.action}</code>
              </td>
              <td>
                <Badge tone={e.outcome === "success" ? "success" : "danger"}>{e.outcome}</Badge>
              </td>
              <td className="path">{e.target ?? (e.run_id ? e.run_id.slice(-12) : "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
