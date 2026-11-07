import { ExternalLink, GitBranch, Pencil, Plus, Rocket, ShieldAlert, ShieldCheck, Trash2, Webhook } from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { errorText } from "../api";
import {
  ACTIVE_STATES,
  type Deployment,
  type Environment,
  type EnvironmentInput,
  type EnvironmentKind,
  STATE_TEXT,
  deployApi,
  refreshDeploy,
  useDeployments,
  useEnvironments,
} from "../data/deploy";
import { useGitHubSettings, useGitHubWorkflows } from "../data/github";
import { useProjects } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import { SecretField } from "../ui/SecretField";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Dialog,
  duration,
  EmptyState,
  ErrorState,
  LinkButton,
  PageHeader,
  QueryState,
  relativeTime,
  Segmented,
  Select,
  Skeleton,
  toast,
} from "../ui";
import { integrationsHref } from "./GitHub";
import "./deploy.css";

// Deployments of the current project (ADR-0048): environments with their latest
// deployment, a deploy (production waits for an owner's or a maintainer's approval), the
// history, and the environments' editor for those who manage the project.

function StateTag({ d }: { d: Deployment }) {
  const s = STATE_TEXT[d.state] ?? { label: d.state, tone: "neutral" as const };
  return (
    <Badge tone={s.tone} dot>
      {s.label}
    </Badge>
  );
}

function short(commit: string | null): string {
  return commit ? commit.slice(0, 7) : "—";
}

function Links({ d }: { d: Deployment }) {
  return (
    <span className="row dep-links">
      {d.run_id && (
        <a href={href("run", d.run_id)} aria-label="Run деплоя">
          run
        </a>
      )}
      {d.external_url && (
        <a href={d.external_url} target="_blank" rel="noreferrer" aria-label="Деплой во внешней системе">
          <ExternalLink size={12} />
        </a>
      )}
    </span>
  );
}

// ------------------------------------------------------------------ deploy --
function DeployDialog({
  projectId,
  env,
  defaultRef,
  onClose,
}: {
  projectId: string;
  env: Environment | null;
  defaultRef: string;
  onClose: () => void;
}) {
  const [ref, setRef] = useState(defaultRef);
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setRef(defaultRef);
    setConfirm("");
  }, [env, defaultRef]);
  if (!env) return null;
  const waits = env.production || env.requires_approval;
  const confirmed = !env.production || confirm.trim() === env.name;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!env || !confirmed) return;
    setBusy(true);
    try {
      const d = await deployApi.deploy(projectId, env.id, ref.trim());
      toast({
        tone: "success",
        title: `Деплой в ${env.name} запущен`,
        text: waits ? "Ждёт подтверждения владельца или мейнтейнера на странице run'а." : `${ref} → ${env.name}`,
      });
      onClose();
      if (d.run_id && waits) window.location.hash = href("run", d.run_id);
    } catch (err) {
      toast({ tone: "danger", title: "Деплой не запущен", text: errorText(err) });
    } finally {
      setBusy(false);
      refreshDeploy(projectId);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Деплой в ${env.name}`}
      description={env.kind === "github_actions" ? `GitHub Actions: ${env.workflow}` : "Подписанный webhook"}
      footer={
        <Button type="submit" form="dep-deploy" variant={env.production ? "danger" : "primary"} icon={<Rocket size={14} />} loading={busy} disabled={!ref.trim() || !confirmed}>
          Задеплоить
        </Button>
      }
    >
      <form id="dep-deploy" className="dep-form" onSubmit={(e) => void submit(e)}>
        <label className="dep-field">
          <span className="field-label">Ветка, тег или коммит</span>
          <input className="input" value={ref} onChange={(e) => setRef(e.target.value)} required autoFocus />
        </label>
        {env.require_gates && <p className="text-muted dep-note">Сначала проверяются блокирующие Quality Gates последнего завершённого аудита.</p>}
        {waits && (
          <p className="dep-warn" role="note">
            <ShieldAlert size={14} aria-hidden /> {env.production ? "Production: " : ""}деплой дождётся подтверждения (approve) владельца или мейнтейнера проекта — до
            него ничего не запускается.
          </p>
        )}
        {env.production && (
          <label className="dep-field">
            <span className="field-label">
              Для подтверждения введите имя среды: <strong>{env.name}</strong>
            </span>
            <input className="input" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" aria-label="Имя среды для подтверждения" />
          </label>
        )}
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ editor --
const BLANK: EnvironmentInput = {
  name: "",
  kind: "webhook",
  production: false,
  requires_approval: false,
  require_gates: false,
  workflow: "",
  environment_input: "environment",
  ref_input: "",
  webhook_url: "",
  health_url: "",
  timeout_minutes: 30,
};

function toInput(env: Environment): EnvironmentInput {
  return {
    name: env.name,
    kind: env.kind,
    production: env.production,
    requires_approval: env.requires_approval && !env.production,
    require_gates: env.require_gates,
    position: env.position,
    workflow: env.workflow ?? "",
    environment_input: env.environment_input ?? "",
    ref_input: env.ref_input ?? "",
    webhook_url: env.webhook_url ?? "",
    health_url: env.health_url ?? "",
    timeout_minutes: env.timeout_minutes,
  };
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <label className="dep-field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <small className="text-muted">{hint}</small>}
    </label>
  );
}

function EnvironmentEditor({
  projectId,
  editing,
  githubReady,
  onClose,
}: {
  projectId: string;
  editing: Environment | "new";
  githubReady: boolean;
  onClose: () => void;
}) {
  const [env, setEnv] = useState<Environment | null>(editing === "new" ? null : editing);
  const [form, setForm] = useState<EnvironmentInput>(editing === "new" ? BLANK : toInput(editing));
  const [busy, setBusy] = useState(false);
  const workflows = useGitHubWorkflows(projectId, githubReady && form.kind === "github_actions");
  const set = <K extends keyof EnvironmentInput>(key: K, value: EnvironmentInput[K]) => setForm((f) => ({ ...f, [key]: value }));

  function body(): EnvironmentInput {
    const clean = (v: string | null | undefined) => (v ?? "").trim() || null;
    return {
      ...form,
      name: form.name.trim(),
      workflow: form.kind === "github_actions" ? clean(form.workflow) : null,
      environment_input: form.kind === "github_actions" ? clean(form.environment_input) : null,
      ref_input: form.kind === "github_actions" ? clean(form.ref_input) : null,
      webhook_url: form.kind === "webhook" ? clean(form.webhook_url) : null,
      health_url: clean(form.health_url),
    };
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const saved = env ? await deployApi.updateEnvironment(projectId, env.id, body()) : await deployApi.createEnvironment(projectId, body());
      toast({ tone: "success", title: env ? "Среда сохранена" : "Среда создана", text: saved.name });
      if (!env && saved.kind === "webhook") {
        setEnv(saved); // stay open: the webhook's signing secret is set next
        setForm(toInput(saved));
      } else {
        onClose();
      }
    } catch (err) {
      toast({ tone: "danger", title: "Не сохранено", text: errorText(err) });
    } finally {
      setBusy(false);
      refreshDeploy(projectId);
    }
  }

  async function remove() {
    if (!env || !window.confirm(`Удалить среду «${env.name}» вместе с историей её деплоев и секретом webhook?`)) return;
    setBusy(true);
    try {
      await deployApi.deleteEnvironment(projectId, env.id);
      toast({ tone: "success", title: "Среда удалена", text: env.name });
      onClose();
    } catch (err) {
      toast({ tone: "danger", title: "Не удалено", text: errorText(err) });
    } finally {
      setBusy(false);
      refreshDeploy(projectId);
    }
  }

  const files = (workflows.data ?? []).filter((w) => w.state === "active").map((w) => w.file);
  return (
    <Dialog
      open
      wide
      onOpenChange={(o) => !o && onClose()}
      title={env ? `Среда ${env.name}` : "Новая среда"}
      footer={
        <>
          {env && (
            <Button variant="ghost" icon={<Trash2 size={14} />} disabled={busy} onClick={() => void remove()}>
              Удалить
            </Button>
          )}
          <span className="card-spacer" />
          <Button type="submit" form="dep-env" variant="primary" loading={busy} disabled={!form.name.trim()}>
            Сохранить
          </Button>
        </>
      }
    >
      <form id="dep-env" className="dep-form dep-form-grid" onSubmit={(e) => void save(e)}>
        <Field label="Имя">
          <input className="input" value={form.name} placeholder="staging" onChange={(e) => set("name", e.target.value)} required maxLength={64} />
        </Field>
        <div className="dep-field">
          <span className="field-label">Как деплоить</span>
          <Segmented<EnvironmentKind>
            label="Способ деплоя"
            value={form.kind}
            onChange={(k) => set("kind", k)}
            options={[
              { value: "webhook", label: "Webhook" },
              { value: "github_actions", label: "GitHub Actions" },
            ]}
          />
        </div>
        {form.kind === "github_actions" ? (
          <>
            {!githubReady && (
              <p className="dep-warn dep-wide" role="note">
                <GitBranch size={14} aria-hidden /> GitHub Actions проекта не подключены или выключены —{" "}
                <a href={integrationsHref(projectId)}>настройте во вкладке «Интеграции»</a>.
              </p>
            )}
            <Field label="Workflow (файл с workflow_dispatch)" hint="Например deploy.yml из .github/workflows.">
              <input className="input" list="dep-workflows" value={form.workflow ?? ""} placeholder="deploy.yml" onChange={(e) => set("workflow", e.target.value)} required />
              <datalist id="dep-workflows">
                {files.map((f) => (
                  <option key={f} value={f} />
                ))}
              </datalist>
            </Field>
            <Field label="Вход для имени среды" hint="Пусто — не передавать.">
              <input className="input" value={form.environment_input ?? ""} placeholder="environment" onChange={(e) => set("environment_input", e.target.value)} />
            </Field>
            <Field label="Вход для ref" hint="Пусто — не передавать (ref и так задаёт ветку запуска).">
              <input className="input" value={form.ref_input ?? ""} placeholder="ref" onChange={(e) => set("ref_input", e.target.value)} />
            </Field>
            <Field label="Таймаут, минут">
              <input className="input" type="number" min={1} max={120} value={form.timeout_minutes ?? 30} onChange={(e) => set("timeout_minutes", Number(e.target.value) || 30)} />
            </Field>
          </>
        ) : (
          <Field label="Адрес webhook (https, публичный)" hint="Тело — JSON о деплое; заголовок X-AI-Coding-Signature: sha256=HMAC тела секретом среды.">
            <input className="input" type="url" value={form.webhook_url ?? ""} placeholder="https://deploy.example.com/hooks/ai" onChange={(e) => set("webhook_url", e.target.value)} required />
          </Field>
        )}
        <Field label="Проверка здоровья после деплоя (https, необязательно)" hint="GET должен ответить 2xx; несколько попыток.">
          <input className="input" type="url" value={form.health_url ?? ""} placeholder="https://staging.example.com/healthz" onChange={(e) => set("health_url", e.target.value)} />
        </Field>
        <div className="dep-field dep-wide dep-checks">
          <label className="check">
            <input type="checkbox" checked={form.production} onChange={(e) => set("production", e.target.checked)} /> Production (всегда с подтверждением и вводом имени)
          </label>
          <label className="check">
            <input type="checkbox" checked={form.production || form.requires_approval} disabled={form.production} onChange={(e) => set("requires_approval", e.target.checked)} />{" "}
            Требовать подтверждение
          </label>
          <label className="check">
            <input type="checkbox" checked={form.require_gates} onChange={(e) => set("require_gates", e.target.checked)} /> Только при пройденных блокирующих Quality Gates
          </label>
        </div>
        {env?.kind === "webhook" && env.secret_name && (
          <div className="dep-wide">
            <SecretField projectId={projectId} name={env.secret_name} label="Секрет подписи webhook" help={`Хранится зашифрованным как ${env.secret_name}; без него деплой не отправляется.`} />
          </div>
        )}
        {!env && form.kind === "webhook" && <p className="text-muted dep-note dep-wide">Секрет подписи webhook задаётся сразу после создания среды.</p>}
      </form>
    </Dialog>
  );
}

// -------------------------------------------------------------------- page --
function EnvironmentCard({
  env,
  canDeploy,
  canManage,
  githubReady,
  onDeploy,
  onEdit,
}: {
  env: Environment;
  canDeploy: boolean;
  canManage: boolean;
  githubReady: boolean;
  onDeploy: () => void;
  onEdit: () => void;
}) {
  const last = env.last_deployment;
  const busy = last !== null && ACTIVE_STATES.has(last.state);
  const blocked = env.kind === "github_actions" && !githubReady;
  return (
    <section className="dep-env" aria-label={`Среда ${env.name}`}>
      <div className="row">
        {env.kind === "github_actions" ? <GitBranch size={15} aria-hidden /> : <Webhook size={15} aria-hidden />}
        <strong className="dep-env-name">{env.name}</strong>
        {env.production && <Badge tone="danger">production</Badge>}
        <span className="card-spacer" />
        {canManage && (
          <Button size="sm" variant="ghost" icon={<Pencil size={13} />} aria-label={`Изменить среду ${env.name}`} onClick={onEdit} />
        )}
      </div>
      <div className="row dep-tags">
        <Badge>{env.kind === "github_actions" ? `GitHub Actions · ${env.workflow}` : "webhook"}</Badge>
        {env.requires_approval && <Badge tone="warning">с подтверждением</Badge>}
        {env.require_gates && (
          <Badge tone="info">
            <ShieldCheck size={11} aria-hidden /> gates
          </Badge>
        )}
        {blocked && <Badge tone="warning">GitHub не готов</Badge>}
      </div>
      {last ? (
        <dl className="kv dep-last">
          <dt>Последний</dt>
          <dd>
            <StateTag d={last} />
          </dd>
          <dt>Версия</dt>
          <dd>
            <code>{last.ref}</code> <span className="mono text-muted">{short(last.commit)}</span>
          </dd>
          <dt>Кто, когда</dt>
          <dd>
            {last.requested_by}, {relativeTime(last.created_at)} <Links d={last} />
          </dd>
          {last.state === "failed" && last.detail && (
            <>
              <dt>Причина</dt>
              <dd className="dep-detail">{last.detail}</dd>
            </>
          )}
        </dl>
      ) : (
        <p className="text-muted">Деплоев ещё не было.</p>
      )}
      {canDeploy && (
        <Button variant={env.production ? "secondary" : "primary"} icon={<Rocket size={14} />} disabled={busy || blocked} onClick={onDeploy} title={busy ? "Деплой уже идёт" : undefined}>
          {busy ? "Идёт деплой…" : "Задеплоить"}
        </Button>
      )}
    </section>
  );
}

function History({ projectId }: { projectId: string }) {
  const history = useDeployments(projectId);
  return (
    <Card title="История деплоев" icon={<Rocket size={15} />}>
      <QueryState query={history} skeleton={<Skeleton lines={4} />}>
        {(rows) => (
          <DataTable
            label="История деплоев"
            rows={rows}
            rowKey={(d) => d.id}
            columns={[
              { id: "when", header: "Когда", cell: (d) => relativeTime(d.created_at), sort: (d) => d.created_at ?? "" },
              { id: "env", header: "Среда", cell: (d) => d.environment ?? "—", sort: (d) => d.environment ?? "" },
              {
                id: "ref",
                header: "Версия",
                cell: (d) => (
                  <>
                    <code>{d.ref}</code> <span className="mono text-muted">{short(d.commit)}</span>
                  </>
                ),
              },
              { id: "state", header: "Состояние", cell: (d) => <StateTag d={d} />, sort: (d) => d.state },
              { id: "who", header: "Кто", cell: (d) => d.requested_by, sort: (d) => d.requested_by },
              { id: "took", header: "Длительность", cell: (d) => (d.finished_at ? duration(d.created_at, d.finished_at) : "—") },
              { id: "links", header: "", cell: (d) => <Links d={d} /> },
            ]}
            empty={<EmptyState icon={<Rocket size={20} />} title="Деплоев ещё не было" />}
          />
        )}
      </QueryState>
    </Card>
  );
}

function ProjectDeployments({ projectId }: { projectId: string }) {
  const envs = useEnvironments(projectId);
  const github = useGitHubSettings(projectId);
  const githubReady = Boolean(github.data?.enabled && github.data.token_set && github.data.data.repository);
  const [deploying, setDeploying] = useState<Environment | null>(null);
  const [editing, setEditing] = useState<Environment | "new" | null>(null);
  if (envs.isLoading) return <Skeleton lines={6} />;
  if (envs.error || !envs.data) return <ErrorState error={envs.error} onRetry={() => void envs.refetch()} />;
  const { environments, can_manage: canManage, can_deploy: canDeploy } = envs.data;
  return (
    <div className="stack">
      <Card
        title="Среды"
        icon={<Rocket size={15} />}
        actions={
          canManage && (
            <Button size="sm" icon={<Plus size={13} />} onClick={() => setEditing("new")}>
              Добавить среду
            </Button>
          )
        }
      >
        {environments.length === 0 ? (
          <EmptyState icon={<Rocket size={20} />} title="Сред пока нет">
            {canManage
              ? "Добавьте среду: GitHub Actions (workflow с workflow_dispatch) или подписанный webhook вашей CD-системы."
              : "Среды добавляют владелец и мейнтейнеры проекта."}
          </EmptyState>
        ) : (
          <div className="dep-grid">
            {environments.map((env) => (
              <EnvironmentCard
                key={env.id}
                env={env}
                canDeploy={canDeploy}
                canManage={canManage}
                githubReady={githubReady}
                onDeploy={() => setDeploying(env)}
                onEdit={() => setEditing(env)}
              />
            ))}
          </div>
        )}
        {!canDeploy && environments.length > 0 && <p className="text-muted dep-note">Деплоить могут владелец и мейнтейнеры проекта.</p>}
      </Card>
      <History projectId={projectId} />
      <DeployDialog projectId={projectId} env={deploying} defaultRef={github.data?.data.default_ref || "main"} onClose={() => setDeploying(null)} />
      {editing && <EnvironmentEditor projectId={projectId} editing={editing} githubReady={githubReady} onClose={() => setEditing(null)} />}
    </div>
  );
}

export default function Deployments() {
  const projectId = useUi((s) => s.projectId);
  const setProject = useUi((s) => s.setProject);
  const projects = useProjects();
  const list = (projects.data ?? []).filter((p) => p.role);
  const current = list.find((p) => p.id === projectId);
  return (
    <div className="page">
      <PageHeader
        icon={<Rocket size={18} />}
        title="Деплой и среды"
        subtitle="Среды проекта, деплой через GitHub Actions или подписанный webhook, подтверждение Production и история."
        actions={
          list.length > 0 && (
            <Select
              label="Проект"
              hideLabel
              value={current?.id ?? ""}
              onChange={(id) => setProject(id || null)}
              options={[...(current ? [] : [{ value: "", label: "Выберите проект" }]), ...list.map((p) => ({ value: p.id, label: p.name }))]}
            />
          )
        }
      />
      {projects.isLoading ? (
        <Skeleton lines={4} />
      ) : !current ? (
        <Card>
          <EmptyState
            icon={<Rocket size={20} />}
            title={list.length ? "Выберите проект" : "Нет проектов"}
            action={!list.length && <LinkButton href={href("new")}>Создать проект</LinkButton>}
          >
            {list.length ? "Деплой настраивается для каждого проекта отдельно." : "Деплой появится, когда у вас будет проект."}
          </EmptyState>
        </Card>
      ) : (
        <ProjectDeployments key={current.id} projectId={current.id} />
      )}
    </div>
  );
}
