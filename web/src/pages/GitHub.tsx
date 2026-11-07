import { CheckCircle2, ExternalLink, GitBranch, Play, PlugZap, RefreshCw, Settings2, XCircle } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { errorText } from "../api";
import { useProjects } from "../data/queries";
import {
  type GitHubCheck,
  type GitHubLast,
  type GitHubProjectStatus,
  type GitHubRun,
  githubApi,
  reasonText,
  refreshGitHub,
  runTone,
  useGitHubJobs,
  useGitHubRuns,
  useGitHubSettings,
  useGitHubStatus,
  useGitHubWorkflows,
} from "../data/github";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import { SecretField } from "../ui/SecretField";
import { Badge, Button, Card, Dialog, EmptyState, ErrorState, LinkButton, QueryState, relativeTime, Select, Skeleton, toast } from "../ui";
import "./deploy.css";

// GitHub Actions of the projects, through the server (ADR-0043): the project's
// «Интеграции» tab connects and turns it on; the infrastructure page shows every
// visible project's connection and the current project's workflow runs.

const CAN_DISPATCH = new Set(["owner", "maintainer", "developer"]);

/** Where a project's GitHub connection is set up. */
export const integrationsHref = (projectId: string) => href("project", projectId, { tab: "integrations" });

function seconds(n: number | null): string {
  if (n === null) return "—";
  return n < 60 ? `${n} с` : `${Math.floor(n / 60)} мин ${n % 60} с`;
}

function LastCall({ last }: { last: GitHubLast | null }) {
  if (!last) return <span className="text-muted">GitHub ещё не опрашивался</span>;
  const what = last.kind === "check" ? "проверка" : "запрос";
  return last.ok ? (
    <span className="text-muted">
      {what} {relativeTime(last.at)}: успешно
    </span>
  ) : (
    <span className="gh-error">
      {what} {relativeTime(last.at)}: {reasonText(last.reason, last.error)}
    </span>
  );
}

// ------------------------------------------------------- «Интеграции» tab --
function CheckResult({ result }: { result: GitHubCheck }) {
  if (!result.ok) {
    return (
      <div className="gh-check gh-check-bad" role="status">
        <XCircle size={16} aria-hidden />
        <div>
          <strong>Подключение не работает.</strong> {reasonText(result.reason, result.error)}
          {result.error && <p className="text-muted gh-detail">{result.error}</p>}
        </div>
      </div>
    );
  }
  const perms = Object.entries(result.permissions ?? {}).filter(([, on]) => on).map(([name]) => name);
  return (
    <div className="gh-check gh-check-ok" role="status">
      <CheckCircle2 size={16} aria-hidden />
      <div>
        <strong>Подключение работает:</strong>{" "}
        {result.html_url ? (
          <a href={result.html_url} target="_blank" rel="noreferrer">
            {result.repository}
          </a>
        ) : (
          result.repository
        )}{" "}
        {result.private ? <Badge>приватный</Badge> : <Badge>публичный</Badge>} · ветка по умолчанию <code>{result.default_branch}</code>
        <p className="text-muted gh-detail">Права токена в репозитории: {perms.length ? perms.join(", ") : "только чтение публичных данных"}.</p>
      </div>
    </div>
  );
}

export function GitHubIntegration({ projectId }: { projectId: string }) {
  const settings = useGitHubSettings(projectId);
  const [repository, setRepository] = useState("");
  const [apiUrl, setApiUrl] = useState("");
  const [defaultRef, setDefaultRef] = useState("main");
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<GitHubCheck | null>(null);
  const data = settings.data;

  useEffect(() => {
    if (!data) return;
    setRepository(data.data.repository ?? "");
    setApiUrl(data.data.api_url && data.data.api_url !== "https://api.github.com" ? data.data.api_url : "");
    setDefaultRef(data.data.default_ref ?? "main");
    setEnabled(data.enabled);
  }, [data]);

  if (settings.isLoading) return <Skeleton lines={5} />;
  if (settings.error || !data) return <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />;
  const readOnly = !data.can_manage;

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await githubApi.save(projectId, { enabled, repository: repository.trim(), default_ref: defaultRef.trim() || "main", ...(apiUrl.trim() ? { api_url: apiUrl.trim() } : {}) }, data?.version);
      toast({ tone: "success", title: "Настройки GitHub сохранены", text: enabled ? "GitHub Actions включены." : "GitHub Actions выключены: сервер не обращается к GitHub." });
    } catch (err) {
      toast({ tone: "danger", title: "Не сохранено", text: errorText(err) });
    } finally {
      setSaving(false);
      refreshGitHub(projectId);
    }
  }

  async function runCheck() {
    setChecking(true);
    try {
      setCheck(await githubApi.check(projectId));
    } catch (err) {
      setCheck({ ok: false, checked_at: new Date().toISOString(), error: errorText(err) });
    } finally {
      setChecking(false);
      refreshGitHub(projectId);
    }
  }

  return (
    <div className="stack">
      <Card
        title="GitHub Actions"
        icon={<GitBranch size={15} />}
        actions={data.enabled ? <Badge tone="success" dot>включены</Badge> : <Badge dot>выключены</Badge>}
      >
        <form className="gh-form" onSubmit={(e) => void save(e)}>
          <label className="check gh-switch">
            <input type="checkbox" role="switch" checked={enabled} disabled={readOnly || saving} onChange={(e) => setEnabled(e.target.checked)} />
            Включить GitHub Actions
          </label>
          <p className="text-muted gh-detail">
            Выключено — сервер не обращается к GitHub для этого проекта, а деплой через GitHub Actions не запускается. Проверить подключение можно и в выключенном
            состоянии.
          </p>
          <label className="gh-field">
            <span className="field-label">Репозиторий</span>
            <input className="input" value={repository} placeholder="owner/name или https://github.com/owner/name" disabled={readOnly} onChange={(e) => setRepository(e.target.value)} required />
          </label>
          <label className="gh-field">
            <span className="field-label">Ветка по умолчанию</span>
            <input className="input" value={defaultRef} placeholder="main" disabled={readOnly} onChange={(e) => setDefaultRef(e.target.value)} />
          </label>
          <label className="gh-field">
            <span className="field-label">Адрес API (GitHub Enterprise)</span>
            <input className="input" value={apiUrl} placeholder="https://api.github.com" disabled={readOnly} onChange={(e) => setApiUrl(e.target.value)} />
          </label>
          {!readOnly && (
            <div className="row">
              <Button type="submit" variant="primary" loading={saving} disabled={!repository.trim()}>
                Сохранить
              </Button>
              {data.updated_by && (
                <span className="text-muted gh-detail">
                  изменено: {data.updated_by}, {relativeTime(data.updated_at)}
                </span>
              )}
            </div>
          )}
        </form>
        <div className="gh-token">
          {data.can_manage ? (
            <SecretField
              projectId={projectId}
              name="github.token"
              label="Токен GitHub"
              placeholder="github_pat_… или ghp_…"
              help="Fine-grained token на этот репозиторий: Actions — чтение и запись, Contents — чтение. Хранится зашифрованным и не показывается."
            />
          ) : (
            <p className="text-muted gh-detail">Токен: {data.token_set ? "задан" : "не задан"}. Менять настройки могут владелец и мейнтейнеры проекта.</p>
          )}
        </div>
      </Card>
      <Card
        title="Проверка подключения"
        icon={<PlugZap size={15} />}
        actions={
          <Button size="sm" icon={<RefreshCw size={13} />} loading={checking} onClick={() => void runCheck()}>
            Проверить подключение
          </Button>
        }
      >
        {check ? (
          <CheckResult result={check} />
        ) : (
          <p className="text-muted">
            Запрос к GitHub с сохранённым токеном и репозиторием: виден ли репозиторий и какие у токена права. <LastCall last={data.last} />
          </p>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------- infrastructure page --
function projectState(p: GitHubProjectStatus): { label: string; tone: "success" | "danger" | "warning" | "neutral" } {
  if (!p.connected) return { label: p.repository ? "нет токена" : "не подключён", tone: "neutral" };
  if (!p.enabled) return { label: "выключен", tone: "neutral" };
  if (p.last && !p.last.ok) return { label: "ошибка", tone: "danger" };
  return { label: "работает", tone: "success" };
}

/** The «GitHub Actions» tile of the environments grid: the state across projects. */
export function GitHubEnvCard() {
  const status = useGitHubStatus();
  const s = status.data;
  return (
    <div className="env-card" role="listitem">
      <div className="row">
        <GitBranch size={16} aria-hidden />
        <strong>GitHub Actions</strong>
        <span className="card-spacer" />
        {!s ? (
          <Badge>{status.isLoading ? "проверка" : "нет данных"}</Badge>
        ) : s.failing > 0 ? (
          <Badge tone="danger" dot>
            ошибок: {s.failing}
          </Badge>
        ) : s.enabled > 0 ? (
          <Badge tone="success" dot>
            {s.enabled}
          </Badge>
        ) : (
          <Badge>не включены</Badge>
        )}
      </div>
      <small className="text-muted">
        {s
          ? `Подключено проектов: ${s.connected} из ${s.projects.length}, включено: ${s.enabled}. Сервер опрашивает GitHub только для включённых.`
          : "Workflow-запуски проектов через сервер; подключение — во вкладке «Интеграции» проекта."}
      </small>
    </div>
  );
}

function Jobs({ projectId, run }: { projectId: string; run: GitHubRun }) {
  const jobs = useGitHubJobs(projectId, run.id);
  return (
    <QueryState query={jobs} skeleton={<Skeleton lines={2} />}>
      {(list) =>
        list.length === 0 ? (
          <p className="text-muted">У запуска нет заданий.</p>
        ) : (
          <ul className="gh-jobs">
            {list.map((j) => {
              const t = runTone(j);
              return (
                <li key={j.id}>
                  <div className="row">
                    <Badge tone={t.tone} dot>
                      {t.label}
                    </Badge>
                    <strong>{j.name}</strong>
                    {j.html_url && (
                      <a href={j.html_url} target="_blank" rel="noreferrer" aria-label={`Задание ${j.name} на GitHub`}>
                        <ExternalLink size={12} />
                      </a>
                    )}
                  </div>
                  {j.steps.some((st) => st.conclusion === "failure") && (
                    <small className="gh-error">Шаг с ошибкой: {j.steps.filter((st) => st.conclusion === "failure").map((st) => st.name).join(", ")}</small>
                  )}
                </li>
              );
            })}
          </ul>
        )
      }
    </QueryState>
  );
}

function DispatchDialog({ projectId, defaultRef, open, onOpenChange }: { projectId: string; defaultRef: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const workflows = useGitHubWorkflows(projectId, open);
  const [workflow, setWorkflow] = useState("");
  const [ref, setRef] = useState(defaultRef);
  const [inputs, setInputs] = useState("");
  const [busy, setBusy] = useState(false);
  const files = (workflows.data ?? []).filter((w) => w.state === "active");
  useEffect(() => setRef(defaultRef), [defaultRef]);
  useEffect(() => {
    const first = files[0]?.file;
    if (!workflow && first) setWorkflow(first);
  }, [files, workflow]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsed: Record<string, string> = {};
    for (const line of inputs.split("\n").map((l) => l.trim()).filter(Boolean)) {
      const at = line.indexOf("=");
      if (at < 1) {
        toast({ tone: "danger", title: "Входные параметры", text: `Ожидается «имя=значение»: ${line}` });
        return;
      }
      parsed[line.slice(0, at).trim()] = line.slice(at + 1).trim();
    }
    setBusy(true);
    try {
      await githubApi.dispatch(projectId, workflow, ref.trim(), parsed);
      toast({ tone: "success", title: "Workflow запущен", text: `${workflow} на ${ref}` });
      onOpenChange(false);
      refreshGitHub(projectId);
    } catch (err) {
      toast({ tone: "danger", title: "Workflow не запущен", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Запустить workflow"
      description="workflow_dispatch через сервер; запуск записывается в журнал проекта."
      footer={
        <Button type="submit" form="gh-dispatch" variant="primary" icon={<Play size={14} />} loading={busy} disabled={!workflow || !ref.trim()}>
          Запустить
        </Button>
      }
    >
      <form id="gh-dispatch" className="gh-form" onSubmit={(e) => void submit(e)}>
        {workflows.isLoading ? (
          <Skeleton lines={1} />
        ) : workflows.error ? (
          <ErrorState error={workflows.error} />
        ) : (
          <Select label="Workflow" value={workflow} onChange={setWorkflow} options={files.map((w) => ({ value: w.file, label: `${w.name} (${w.file})` }))} />
        )}
        <label className="gh-field">
          <span className="field-label">Ветка или тег</span>
          <input className="input" value={ref} onChange={(e) => setRef(e.target.value)} required />
        </label>
        <label className="gh-field">
          <span className="field-label">Входные параметры (по одному «имя=значение» в строке)</span>
          <textarea className="input" rows={3} value={inputs} onChange={(e) => setInputs(e.target.value)} />
        </label>
      </form>
    </Dialog>
  );
}

function Runs({ project }: { project: GitHubProjectStatus }) {
  const runs = useGitHubRuns(project.project_id, project.connected && project.enabled);
  const settings = useGitHubSettings(project.project_id);
  const projects = useProjects();
  const role = projects.data?.find((p) => p.id === project.project_id)?.role ?? "";
  const [open, setOpen] = useState<number | null>(null);
  const [dispatching, setDispatching] = useState(false);
  if (!project.connected || !project.enabled) {
    return (
      <EmptyState
        icon={<GitBranch size={20} />}
        title={project.connected ? "GitHub Actions выключены" : "Проект не подключён к GitHub"}
        action={
          <LinkButton href={integrationsHref(project.project_id)} icon={<Settings2 size={14} />}>
            Настроить
          </LinkButton>
        }
      >
        {project.connected ? "Включите их во вкладке «Интеграции» проекта — тогда сервер покажет запуски." : "Укажите репозиторий и токен во вкладке «Интеграции» проекта."}
      </EmptyState>
    );
  }
  return (
    <div className="stack">
      <div className="row">
        <span className="text-muted">
          {project.repository} · <LastCall last={project.last} />
        </span>
        <span className="card-spacer" />
        {CAN_DISPATCH.has(role) && (
          <Button size="sm" icon={<Play size={13} />} onClick={() => setDispatching(true)}>
            Запустить workflow
          </Button>
        )}
      </div>
      <QueryState query={runs} skeleton={<Skeleton lines={4} />}>
        {(list) =>
          list.length === 0 ? (
            <p className="text-muted">Запусков пока нет.</p>
          ) : (
            <ul className="gh-runs" aria-label="Запуски GitHub Actions">
              {list.map((r) => {
                const t = runTone(r);
                return (
                  <li key={r.id} className="gh-run">
                    <div className="gh-run-head">
                      <Badge tone={t.tone} dot>
                        {t.label}
                      </Badge>
                      <span className="gh-run-title">{r.title || r.name}</span>
                      <span className="text-muted gh-run-meta">
                        {r.name} #{r.run_number} · <code>{r.branch}</code> {r.sha?.slice(0, 7)} · {r.actor ?? "—"} · {relativeTime(r.started_at)} ·{" "}
                        {seconds(r.duration_seconds)}
                      </span>
                      <span className="card-spacer" />
                      <Button size="sm" variant="ghost" aria-expanded={open === r.id} onClick={() => setOpen(open === r.id ? null : r.id)}>
                        {open === r.id ? "Скрыть задания" : "Задания"}
                      </Button>
                      {r.html_url && (
                        <a className="btn btn-ghost btn-sm" href={r.html_url} target="_blank" rel="noreferrer" aria-label={`Запуск ${r.run_number} на GitHub`}>
                          <ExternalLink size={13} />
                        </a>
                      )}
                    </div>
                    {open === r.id && <Jobs projectId={project.project_id} run={r} />}
                  </li>
                );
              })}
            </ul>
          )
        }
      </QueryState>
      <DispatchDialog projectId={project.project_id} defaultRef={settings.data?.data.default_ref ?? "main"} open={dispatching} onOpenChange={setDispatching} />
    </div>
  );
}

/** The «GitHub Actions» panel: every visible project's connection, and the runs of one. */
export function GitHubActionsPanel() {
  const status = useGitHubStatus();
  const current = useUi((s) => s.projectId);
  const [chosen, setChosen] = useState<string | null>(null);
  const projects = useMemo(() => status.data?.projects ?? [], [status.data]);
  const shown = projects.find((p) => p.project_id === (chosen ?? current)) ?? projects.find((p) => p.connected && p.enabled) ?? projects[0];
  return (
    <Card title="GitHub Actions" icon={<GitBranch size={15} />}>
      <QueryState
        query={status}
        skeleton={<Skeleton lines={4} />}
        empty={(s) =>
          s.projects.length === 0 ? (
            <p className="text-muted">У вас нет проектов. Подключение к GitHub настраивается во вкладке «Интеграции» проекта.</p>
          ) : null
        }
      >
        {(s) => (
          <div className="stack">
            <ul className="gh-projects" aria-label="Подключение проектов к GitHub">
              {s.projects.map((p) => {
                const st = projectState(p);
                return (
                  <li key={p.project_id}>
                    <Badge tone={st.tone} dot>
                      {st.label}
                    </Badge>
                    <strong>{p.name}</strong>
                    <span className="text-muted mono">{p.repository ?? "—"}</span>
                    {p.enabled && p.last && !p.last.ok && <span className="gh-error">{reasonText(p.last.reason, p.last.error)}</span>}
                    <span className="card-spacer" />
                    <a className="btn btn-ghost btn-sm" href={integrationsHref(p.project_id)}>
                      <Settings2 size={13} aria-hidden /> {p.can_manage ? "Настроить" : "Подробнее"}
                    </a>
                  </li>
                );
              })}
            </ul>
            {shown && (
              <>
                {s.projects.length > 1 && (
                  <Select
                    label="Запуски проекта"
                    value={shown.project_id}
                    onChange={setChosen}
                    options={s.projects.map((p) => ({ value: p.project_id, label: p.name }))}
                  />
                )}
                <Runs key={shown.project_id} project={shown} />
              </>
            )}
          </div>
        )}
      </QueryState>
    </Card>
  );
}
