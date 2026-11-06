import { useQueries } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  Coins,
  FlaskConical,
  FolderGit2,
  FolderPlus,
  ListChecks,
  Rocket,
  Server,
  ShieldCheck,
  Sparkles,
  SquareCode,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip as ChartTooltip, XAxis } from "recharts";
import { api, errorText, type FindingRow, type GlobalRun, type Me } from "../api";
import { queryClient, useEvents, useFindings, useProjects, useReadiness, useRuns, useSystem, useUsage } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  formatCost,
  formatNumber,
  GateBadge,
  isActiveRun,
  LinkButton,
  PageHeader,
  QueryState,
  relativeTime,
  RUN_STATES,
  SEVERITIES,
  SeverityBadge,
  Skeleton,
  StateBadge,
  Stat,
  StatusDot,
  toast,
} from "../ui";

// Mission Control: every number comes from the API and links to the section that shows
// it in full, with the same filter applied.

const QUALITY_SOURCES = new Set(["ruff", "mypy", "eslint", "tsc", "phpstan", "pint", "golangci-lint", "clippy"]);

function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
}

function SeverityBar({ findings }: { findings: FindingRow[] }) {
  const open = findings.filter((f) => f.status === "open");
  const counts = SEVERITIES.map((s) => ({ s, n: open.filter((f) => f.severity.toLowerCase() === s).length }));
  const total = open.length || 1;
  return (
    <div className="stack">
      <div className="sev-bar" aria-hidden>
        {counts.map(({ s, n }) => n > 0 && <span key={s} className={`sev-seg sev-${s}`} style={{ flexGrow: n / total }} />)}
      </div>
      <div className="sev-legend">
        {counts.map(({ s, n }) => (
          <a key={s} href={href("audit", undefined, { severity: s })} className={n ? "sev-item" : "sev-item zero"}>
            <SeverityBadge severity={s} />
            <span className="mono">{n}</span>
          </a>
        ))}
      </div>
    </div>
  );
}

function RunList({ runs, empty }: { runs: GlobalRun[]; empty: string }) {
  if (runs.length === 0) return <p className="text-muted tile-empty">{empty}</p>;
  return (
    <ul className="tile-list">
      {runs.map((r) => (
        <li key={r.id}>
          <a href={href("run", r.id)} className="tile-row">
            <StateBadge state={r.state} />
            <span className="tile-row-main truncate">{r.goal || r.kind}</span>
            <span className="tile-row-meta truncate">{r.project_name}</span>
            <span className="tile-row-time">{relativeTime(r.created_at)}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function GatesTile({ runs }: { runs: GlobalRun[] }) {
  const audits = useMemo(() => {
    const seen = new Set<string>();
    return runs.filter((r) => r.kind === "audit" && r.state === "COMPLETED" && !seen.has(r.project_id) && seen.add(r.project_id)).slice(0, 4);
  }, [runs]);
  const details = useQueries({ queries: audits.map((r) => ({ queryKey: ["run", r.id], queryFn: () => api.run(r.id), staleTime: 60_000 })) });
  if (audits.length === 0) return <p className="text-muted tile-empty">Пока нет завершённых аудитов — Quality Gates появятся после первого.</p>;
  return (
    <ul className="tile-list">
      {audits.map((r, i) => {
        const gates = details[i]?.data?.gates ?? null;
        return (
          <li key={r.id}>
            <a href={href("run", r.id)} className="tile-row gates-row">
              <span className="tile-row-main truncate">{r.project_name}</span>
              <span className="gates-inline">
                {gates === null ? (
                  <span className="text-muted">{details[i]?.isLoading ? "…" : "нет данных"}</span>
                ) : (
                  gates.map((g) => (
                    <span key={g.gate} className="gate-chip" title={`${g.gate}: ${g.result}${g.reason ? ` — ${g.reason}` : ""}`}>
                      <span className="gate-name">{g.gate}</span>
                      <GateBadge result={g.result} />
                    </span>
                  ))
                )}
              </span>
            </a>
          </li>
        );
      })}
    </ul>
  );
}

function UsageTile({ projectId }: { projectId: string | null }) {
  const usage = useUsage(projectId);
  if (!projectId) return <p className="text-muted tile-empty">Выберите проект в меню слева.</p>;
  return (
    <QueryState query={usage} skeleton={<Skeleton height={90} />}>
      {(u) =>
        u.totals.calls === 0 ? (
          <p className="text-muted tile-empty">
            Агенты оркестратора ещё не обращались к моделям. Чат Claude Code / Codex работает по вашей подписке и здесь не учитывается.
          </p>
        ) : (
          <div className="stack">
            <div className="stats">
              <Stat label="Стоимость, 30 дн." value={formatCost(u.totals.cost_usd)} />
              <Stat label="Токены" value={formatNumber(u.totals.input_tokens + u.totals.output_tokens)} />
              <Stat label="Вызовы" value={formatNumber(u.totals.calls)} />
            </div>
            <div className="chart-mini" aria-label="Стоимость по дням">
              <ResponsiveContainer width="100%" height={72}>
                <AreaChart data={u.daily} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="cost-fill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0" stopColor="var(--accent-primary)" stopOpacity={0.45} />
                      <stop offset="1" stopColor="var(--accent-primary)" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="date" hide />
                  <ChartTooltip
                    contentStyle={{ background: "var(--bg-elevated)", border: "1px solid var(--border-secondary)", borderRadius: 8, fontSize: 12 }}
                    formatter={(v) => formatCost(Number(v))}
                  />
                  <Area type="monotone" dataKey="cost_usd" name="Стоимость" stroke="var(--accent-primary)" strokeWidth={1.5} fill="url(#cost-fill)" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )
      }
    </QueryState>
  );
}

function SystemTile({ me }: { me: Me }) {
  const ready = useReadiness();
  const system = useSystem(me.is_admin);
  const ok = ready.data === true;
  return (
    <div className="stack">
      <div className="row">
        <StatusDot tone={ready.isLoading ? "neutral" : ok ? "success" : "danger"} label={ok ? "Сервер и БД доступны" : "Недоступно"} pulse={!ok} />
        <span>{ready.isLoading ? "Проверка…" : ok ? "API и база данных доступны" : "API или база данных недоступны"}</span>
      </div>
      {me.is_admin ? (
        <QueryState query={system} skeleton={<Skeleton lines={2} />}>
          {(s) => (
            <div className="stats">
              <Stat label="В очереди" value={s.queue.queued ?? 0} />
              <Stat label="Worker'ы" value={s.workers.length} tone={s.workers.length ? undefined : "warning"} />
              <Stat
                label="Базы уязвимостей"
                value={`${Object.values(s.analysers).filter(Boolean).length}/${Object.keys(s.analysers).length}`}
                tone={Object.values(s.analysers).every(Boolean) ? "success" : "warning"}
              />
            </div>
          )}
        </QueryState>
      ) : (
        <p className="text-muted">Подробности о очереди и worker'ах видят администраторы.</p>
      )}
    </div>
  );
}

export default function Dashboard({ me }: { me: Me }) {
  const projects = useProjects();
  const runs = useRuns();
  const findings = useFindings();
  const events = useEvents(14);
  const { projectId } = useUi();
  const [auditing, setAuditing] = useState(false);
  const current = projects.data?.find((p) => p.id === projectId) ?? null;
  const allRuns = runs.data ?? [];
  const active = allRuns.filter((r) => isActiveRun(r.state));
  const waiting = allRuns.filter((r) => r.state === "AWAITING_APPROVAL");
  const byState = (states: string[]) => allRuns.filter((r) => states.includes(r.state)).length;
  const quality = (findings.data ?? []).filter((f) => QUALITY_SOURCES.has(f.source) && f.status === "open");
  const qualityBySource = [...new Set(quality.map((f) => f.source))].map((s) => ({ s, n: quality.filter((f) => f.source === s).length })).sort((a, b) => b.n - a.n);

  function fullAudit() {
    if (!current) return;
    setAuditing(true);
    api.createRun(current.id, "audit", {}).then(
      (run) => {
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: "success", title: "Полный аудит запущен", text: current.name });
        window.location.hash = href("run", run.id);
      },
      (err: unknown) => {
        toast({ tone: "danger", title: "Аудит не запущен", text: errorText(err) });
        setAuditing(false);
      },
    );
  }

  return (
    <div className="page">
      <PageHeader
        title={`${greeting()}, ${me.username}`}
        subtitle={
          <>
            Mission Control · {current ? <>проект <strong>{current.name}</strong></> : "проект не выбран"} · <Badge tone="accent">Self-Hosted</Badge>
          </>
        }
        actions={
          <>
            <LinkButton href={href("new")} icon={<FolderPlus size={14} />}>
              Новый проект
            </LinkButton>
            <LinkButton href={current ? href("ai", current.id) : href("workspace")} icon={<Sparkles size={14} />}>
              Новая AI-задача
            </LinkButton>
            <Button variant="primary" icon={<ShieldCheck size={14} />} onClick={fullAudit} loading={auditing} disabled={!current} title={current ? undefined : "Выберите проект"}>
              Полный аудит
            </Button>
          </>
        }
      />

      <div className="bento">
        <Card span={1} title="Проекты" icon={<FolderGit2 size={15} />} href={href("projects")}>
          <QueryState query={projects} skeleton={<Skeleton lines={2} />}>
            {(list) => (
              <div className="stats">
                <Stat label="доступно вам" value={list.length} />
                <Stat label="с активными задачами" value={new Set(active.map((r) => r.project_id)).size} />
              </div>
            )}
          </QueryState>
        </Card>

        <Card span={1} title="Задачи" icon={<ListChecks size={15} />} href={href("tasks")}>
          <QueryState query={runs} skeleton={<Skeleton lines={2} />}>
            {() => (
              <div className="stats">
                <Stat label="в работе" value={active.length} tone={active.length ? undefined : undefined} />
                <Stat label="ждут согласия" value={waiting.length} tone={waiting.length ? "warning" : undefined} />
                <Stat label="с ошибкой" value={byState(["FAILED"])} tone={byState(["FAILED"]) ? "danger" : undefined} />
              </div>
            )}
          </QueryState>
        </Card>

        <Card span={2} title="Безопасность" icon={<ShieldCheck size={15} />} actions={<a href={href("audit")} className="tile-link">Audit Center</a>}>
          <QueryState query={findings} skeleton={<Skeleton lines={2} />}>
            {(list) => <SeverityBar findings={list} />}
          </QueryState>
        </Card>

        <Card span={2} title="Агенты и run'ы в работе" icon={<Bot size={15} />} actions={<a href={href("tasks", undefined, { state: "active" })} className="tile-link">Все</a>}>
          <QueryState query={runs} skeleton={<Skeleton lines={3} />}>
            {() => <RunList runs={active.slice(0, 5)} empty="Сейчас ничего не выполняется." />}
          </QueryState>
        </Card>

        <Card span={2} title="Build & Test · Quality Gates" icon={<FlaskConical size={15} />} actions={<a href={href("testing")} className="tile-link">Тесты</a>}>
          <QueryState query={runs} skeleton={<Skeleton lines={3} />}>
            {(list) => <GatesTile runs={list} />}
          </QueryState>
        </Card>

        <Card span={1} title="Качество кода" icon={<SquareCode size={15} />} href={href("audit", undefined, { category: "quality" })}>
          <QueryState query={findings} skeleton={<Skeleton lines={2} />}>
            {() =>
              quality.length === 0 ? (
                <p className="text-muted tile-empty">Открытых замечаний линтеров и типизации нет.</p>
              ) : (
                <div className="stack">
                  <Stat label="открытых замечаний" value={quality.length} tone="warning" />
                  <div className="row">
                    {qualityBySource.slice(0, 4).map(({ s, n }) => (
                      <Badge key={s}>
                        {s} · {n}
                      </Badge>
                    ))}
                  </div>
                </div>
              )
            }
          </QueryState>
        </Card>

        <Card span={1} title="Деплой" icon={<Rocket size={15} />} href={href("deployments")}>
          <p className="text-muted tile-empty">Не подключён: выпуск делает ваш CI/CD через ветку или PR.</p>
        </Card>

        <Card span={2} title="Модели и стоимость" icon={<Coins size={15} />} actions={<a href={href("models")} className="tile-link">Модели</a>}>
          <UsageTile projectId={projectId} />
        </Card>

        <Card span={2} title="Активность" icon={<Activity size={15} />}>
          <QueryState query={events} skeleton={<Skeleton lines={4} />} empty={(list) => (list.length === 0 ? <EmptyState title="Событий пока нет" /> : null)}>
            {(list) => (
              <ol className="timeline">
                {list.map((e) => (
                  <li key={e.id} className={`outcome-${e.outcome}`}>
                    <span className="timeline-dot" aria-hidden />
                    <span className="timeline-main">
                      <code>{e.action}</code> <span className="text-secondary">{e.actor}</span>
                      {e.target && <span className="text-muted truncate"> · {e.target}</span>}
                    </span>
                    {e.run_id ? (
                      <a href={href("run", e.run_id)} className="timeline-time">
                        {relativeTime(e.created_at)}
                      </a>
                    ) : (
                      <span className="timeline-time">{relativeTime(e.created_at)}</span>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </QueryState>
        </Card>

        <Card span={2} title="Состояние системы" icon={<Server size={15} />} actions={<a href={href("infrastructure")} className="tile-link">Инфраструктура</a>}>
          <SystemTile me={me} />
        </Card>

        <Card span={2} title="Последние задачи" icon={<ListChecks size={15} />} actions={<a href={href("tasks")} className="tile-link">Все задачи</a>}>
          <QueryState query={runs} skeleton={<Skeleton lines={4} />}>
            {(list) => (
              <RunList
                runs={list.filter((r) => !isActiveRun(r.state)).slice(0, 5)}
                empty={`Завершённых задач нет. ${Object.keys(RUN_STATES).length ? "" : ""}`}
              />
            )}
          </QueryState>
        </Card>
      </div>
    </div>
  );
}
