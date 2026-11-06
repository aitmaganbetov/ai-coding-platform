import { Activity, Boxes, Cpu, Database, HardDrive, Laptop, Server, ShieldAlert } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api, type Me } from "../api";
import { useReadiness, useSystem } from "../data/queries";
import { href } from "../shell/routes";
import { Badge, Card, CodeBlock, DataTable, EmptyState, PageHeader, QueryState, relativeTime, Skeleton, StateBadge, Stat, StatusDot } from "../ui";
import { GitHubActionsPanel, GitHubEnvCard } from "./GitHub";
import "./quality.css";

// Infrastructure of the platform: this server (live), its queue and workers, the
// analysers' offline data, and GitHub Actions of the projects (ADR-0043). Environments the
// server cannot observe say so.

export const PULL_COMMAND =
  "docker compose -f deploy/docker-compose.yml exec worker ai-coding tools pull -t semgrep -t gitleaks -t ruff -t mypy -t trivy -t osv-scanner";

function Readiness() {
  const ready = useReadiness();
  const ok = ready.data === true;
  return (
    <div className="row">
      <StatusDot tone={ready.isLoading ? "neutral" : ok ? "success" : "danger"} label={ok ? "Доступен" : "Недоступен"} pulse={!ok && !ready.isLoading} />
      <span>{ready.isLoading ? "Проверка…" : ok ? "API и база данных доступны" : "API или база данных недоступны (/readyz)"}</span>
    </div>
  );
}

function Environments({ workers }: { workers: number | null }) {
  const ready = useReadiness();
  return (
    <div className="env-grid" role="list" aria-label="Среды">
      <div className="env-card" role="listitem">
        <div className="row">
          <Server size={16} aria-hidden />
          <strong>Self-Hosted</strong>
          <span className="card-spacer" />
          <Badge tone={ready.data ? "success" : ready.isLoading ? "neutral" : "danger"} dot>
            {ready.data ? "работает" : ready.isLoading ? "проверка" : "недоступен"}
          </Badge>
        </div>
        <small className="text-muted">Этот сервер: API, очередь, worker'ы, рабочие области.</small>
      </div>
      <LocalEnvCard />
      <GitHubEnvCard />
      <div className="env-card" role="listitem">
        <div className="row">
          <Boxes size={16} aria-hidden />
          <strong>Docker workers</strong>
          <span className="card-spacer" />
          {workers === null ? <Badge>для администраторов</Badge> : <Badge tone={workers ? "success" : "warning"} dot>{workers} в сети</Badge>}
        </div>
        <small className="text-muted">Worker'ы выполняют run'ы в изолированных Docker-песочницах.</small>
      </div>
    </div>
  );
}

/** Developers' machines that report their local `ai-coding` runs (ADR-0047): summaries only. */
function LocalEnvCard() {
  const overview = useQuery({ queryKey: ["local-runs-overview"], queryFn: api.localRunsOverview, refetchInterval: 60_000, retry: false });
  const machines = overview.data?.machines ?? [];
  return (
    <div className="env-card" role="listitem">
      <div className="row">
        <Laptop size={16} aria-hidden />
        <strong>Local (CLI)</strong>
        <span className="card-spacer" />
        {machines.length ? <Badge tone="success" dot>{machines.length} подключено</Badge> : <Badge>не подключено</Badge>}
      </div>
      {overview.error ? (
        <small className="text-muted">Сводки CLI недоступны: сервер ещё не обновлён.</small>
      ) : machines.length === 0 ? (
        <>
          <small className="text-muted">
            <code>ai-coding</code> на машине разработчика можно подключить к серверу: его прогоны (только сводки — без кода и находок) появятся здесь.
          </small>
          <CodeBlock code={`ai-coding server login --url ${window.location.origin}\nai-coding server link <проект>`} label="На машине разработчика" />
          <a href={href("account")} className="tile-link">
            Создать токен
          </a>
        </>
      ) : (
        <>
          <ul className="local-list">
            {machines.slice(0, 5).map((m) => (
              <li key={`${m.machine}-${m.user}`}>
                <span className="truncate">
                  {m.machine} · {m.user} — {m.project ?? "проект"}:
                </span>{" "}
                <StateBadge state={m.state} />{" "}
                {m.verdict && <Badge tone={m.verdict === "PASS" ? "success" : "danger"}>{m.verdict}</Badge>}{" "}
                <span className="text-muted">· {relativeTime(m.reported_at)}</span>
              </li>
            ))}
          </ul>
          <small className="text-muted">{overview.data?.reports_24h ?? 0} за сутки</small>
        </>
      )}
    </div>
  );
}

export default function Infrastructure({ me }: { me: Me }) {
  const system = useSystem(me.is_admin);
  return (
    <div className="page">
      <PageHeader
        icon={<Server size={18} />}
        title="Инфраструктура"
        subtitle="Состояние сервера, очереди и worker'ов, готовность анализаторов и среды выполнения."
      />
      <div className="stack">
        <Card title="Сервер" icon={<Activity size={15} />}>
          <Readiness />
        </Card>
        <GitHubActionsPanel />
        {!me.is_admin ? (
          <>
            <Environments workers={null} />
            <p className="text-muted note">Подробности об очереди, worker'ах и анализаторах видят администраторы сервера.</p>
          </>
        ) : (
          <QueryState query={system} skeleton={<Skeleton lines={6} />}>
            {(s) => {
              const missing = Object.entries(s.analysers).filter(([, ok]) => !ok).map(([name]) => name);
              return (
                <div className="stack">
                  <Environments workers={s.workers.filter((w) => w.online).length} />
                  <div className="bento">
                    <Card span={1} title="Платформа" icon={<Server size={15} />}>
                      <dl className="kv">
                        <dt>Версия</dt>
                        <dd className="mono">{s.version}</dd>
                        <dt>База данных</dt>
                        <dd>
                          <Database size={12} aria-hidden /> {s.database}
                        </dd>
                        <dt>Рабочие области</dt>
                        <dd>{s.workspaces === "on" ? <Badge tone="success">включены</Badge> : <Badge>выключены</Badge>}</dd>
                        <dt>Время сервера</dt>
                        <dd>{new Date(s.time).toLocaleString("ru")}</dd>
                      </dl>
                    </Card>
                    <Card span={1} title="Очередь заданий" icon={<HardDrive size={15} />}>
                      {Object.keys(s.queue).length === 0 ? (
                        <p className="text-muted tile-empty">Очередь пуста.</p>
                      ) : (
                        <div className="stats">
                          {Object.entries(s.queue).map(([state, n]) => (
                            <Stat key={state} label={state} value={n} />
                          ))}
                        </div>
                      )}
                    </Card>
                    <Card span={2} title="Run'ы по состояниям" icon={<Activity size={15} />}>
                      {Object.keys(s.runs).length === 0 ? (
                        <p className="text-muted tile-empty">Run'ов ещё не было.</p>
                      ) : (
                        <ul className="state-counts">
                          {Object.entries(s.runs).map(([state, n]) => (
                            <li key={state}>
                              <StateBadge state={state} />
                              <span className="mono">{n}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </Card>
                  </div>
                  <Card title="Worker'ы" icon={<Cpu size={15} />}>
                    <DataTable
                      label="Worker'ы"
                      rows={s.workers}
                      rowKey={(w) => w.id}
                      columns={[
                        { id: "id", header: "Worker", cell: (w) => <span className="path">{w.id}</span>, sort: (w) => w.id },
                        {
                          id: "state",
                          header: "Состояние",
                          cell: (w) =>
                            !w.online ? (
                              <Badge tone="warning" dot>
                                нет сигнала
                              </Badge>
                            ) : w.active_jobs ? (
                              <Badge tone="info" dot>
                                занят: {w.active_jobs}
                              </Badge>
                            ) : (
                              <Badge tone="success" dot>
                                ждёт заданий
                              </Badge>
                            ),
                          sort: (w) => (w.online ? 1 + w.active_jobs : 0),
                        },
                        { id: "host", header: "Хост", cell: (w) => <span className="path">{w.host ?? "—"}</span> },
                        { id: "started", header: "Запущен", cell: (w) => relativeTime(w.started) },
                        { id: "jobs", header: "Активных заданий", cell: (w) => w.active_jobs, sort: (w) => w.active_jobs },
                        { id: "seen", header: "Последняя активность", cell: (w) => relativeTime(w.last_seen), sort: (w) => w.last_seen ?? "" },
                      ]}
                      empty={
                        <EmptyState icon={<ShieldAlert size={20} />} title="Нет активных worker'ов">
                          Ни один worker не отозвался за последнюю минуту. Проверьте сервис worker: docker compose -f deploy/docker-compose.yml ps worker
                        </EmptyState>
                      }
                    />
                  </Card>
                  <Card title="Анализаторы аудита" icon={<ShieldAlert size={15} />}>
                    <div className="row">
                      {Object.entries(s.analysers).map(([name, ok]) => (
                        <Badge key={name} tone={ok ? "success" : "warning"} dot>
                          {name}: {ok ? "базы уязвимостей готовы" : "нет офлайн-базы"}
                        </Badge>
                      ))}
                    </div>
                    {missing.length > 0 ? (
                      <div className="stack note">
                        <p className="text-secondary">
                          Без офлайн-баз {missing.join(" и ")} пропускаются в аудитах. Скачайте закреплённые образы анализаторов и подготовьте базы (нужен доступ в сеть):
                        </p>
                        <CodeBlock code={PULL_COMMAND} label="на сервере" />
                      </div>
                    ) : (
                      <p className="text-muted note">Офлайн-базы готовы; обновить снимок — та же команда с <code>--refresh</code>.</p>
                    )}
                  </Card>
                </div>
              );
            }}
          </QueryState>
        )}
      </div>
    </div>
  );
}
