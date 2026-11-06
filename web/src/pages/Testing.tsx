import { useQueries } from "@tanstack/react-query";
import { FlaskConical, Gauge, History, ShieldAlert, TestTube2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, type Gate, type GlobalRun, type RunDetail } from "../api";
import { useProjects, useRuns } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import { Card, EmptyState, GateBadge, PageHeader, QueryState, relativeTime, Select, shortId, Skeleton, StateBadge } from "../ui";
import "./quality.css";

// Testing & Quality Gates: what the audits' gates decided, per project and over time.
// Per-test results (JUnit) are not exposed by the API yet: this page says so instead of
// inventing suites.

export const GATES = ["build", "tests", "static", "security", "coverage", "review"] as const;
const GATE_LABEL: Record<string, string> = {
  build: "Сборка",
  tests: "Тесты",
  static: "Статический анализ",
  security: "Безопасность",
  coverage: "Покрытие",
  review: "Ревью",
};

function useDetails(runs: GlobalRun[]) {
  return useQueries({
    queries: runs.map((r) => ({ queryKey: ["run", r.id], queryFn: () => api.run(r.id), staleTime: 60_000 })),
  });
}

function gateOf(detail: RunDetail | undefined, gate: string): Gate | undefined {
  return detail?.gates?.find((g) => g.gate === gate);
}

function LatestPerProject({ audits }: { audits: GlobalRun[] }) {
  const latest = useMemo(() => {
    const seen = new Set<string>();
    return audits.filter((r) => r.state === "COMPLETED" && !seen.has(r.project_id) && seen.add(r.project_id));
  }, [audits]);
  const details = useDetails(latest);
  if (latest.length === 0) return <p className="text-muted tile-empty">Завершённых аудитов ещё нет: Quality Gates появятся после первого.</p>;
  return (
    <div className="table-wrap" role="region" aria-label="Последние Quality Gates по проектам" tabIndex={0}>
      <table className="data-table gate-matrix">
        <thead>
          <tr>
            <th>Проект</th>
            {GATES.map((g) => (
              <th key={g}>{GATE_LABEL[g]}</th>
            ))}
            <th>Аудит</th>
          </tr>
        </thead>
        <tbody>
          {latest.map((r, i) => (
            <tr key={r.id}>
              <td className="cell-main">{r.project_name}</td>
              {GATES.map((g) => {
                const gate = gateOf(details[i]?.data, g);
                return <td key={g}>{gate ? <GateBadge result={gate.result} /> : <span className="text-muted">{details[i]?.isLoading ? "…" : "—"}</span>}</td>;
              })}
              <td>
                <a href={href("run", r.id)}>{relativeTime(r.created_at)}</a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function History_({ runs }: { runs: GlobalRun[] }) {
  const recent = runs.slice(0, 10);
  const details = useDetails(recent);
  const latest = details.find((d) => d.data?.gates)?.data;
  const failed = (latest?.gates ?? []).filter((g) => g.result === "FAIL");
  const testGates = (latest?.gates ?? []).filter((g) => g.gate === "tests" || g.gate === "coverage");
  if (recent.length === 0) return <EmptyState icon={<FlaskConical size={20} />} title="Аудитов этого проекта ещё не было">Запустите «Полный аудит» в Audit Center или Mission Control.</EmptyState>;
  return (
    <div className="stack">
      <Card title="История Quality Gates" icon={<History size={15} />}>
        <div className="table-wrap" role="region" aria-label="История Quality Gates" tabIndex={0}>
          <table className="data-table gate-matrix">
            <thead>
              <tr>
                <th>Аудит</th>
                {GATES.map((g) => (
                  <th key={g}>{GATE_LABEL[g]}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {recent.map((r, i) => (
                <tr key={r.id}>
                  <td>
                    <a href={href("run", r.id)} className="gate-run">
                      <StateBadge state={r.state} />
                      <span className="text-muted">
                        {new Date(r.created_at).toLocaleString("ru")} · {shortId(r.id)}
                      </span>
                    </a>
                  </td>
                  {GATES.map((g) => {
                    const gate = gateOf(details[i]?.data, g);
                    return (
                      <td key={g} title={gate?.reason ?? undefined}>
                        {gate ? <GateBadge result={gate.result} /> : <span className="text-muted">{details[i]?.isLoading ? "…" : "—"}</span>}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <div className="bento">
        <Card span={2} title="Не пройденные gates последнего аудита" icon={<ShieldAlert size={15} />}>
          {!latest ? (
            <p className="text-muted tile-empty">Нет данных о gates.</p>
          ) : failed.length === 0 ? (
            <p className="text-muted tile-empty">Все gates последнего аудита пройдены или пропущены.</p>
          ) : (
            <ul className="gate-failures">
              {failed.map((g) => (
                <li key={g.gate}>
                  <div className="row">
                    <GateBadge result={g.result} />
                    <strong>{GATE_LABEL[g.gate] ?? g.gate}</strong>
                    {g.blocking && <span className="text-muted">блокирует merge</span>}
                  </div>
                  {g.reason && <p className="gate-reason">{g.reason}</p>}
                  {(g.evidence ?? []).length > 0 && (
                    <ul className="gate-evidence">
                      {(g.evidence ?? []).slice(0, 8).map((e) => (
                        <li key={e} className="path">
                          {e}
                        </li>
                      ))}
                    </ul>
                  )}
                  <a href={href("run", latest.id)}>Открыть run</a>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card span={2} title="Тесты и покрытие" icon={<TestTube2 size={15} />}>
          {testGates.length === 0 ? (
            <p className="text-muted tile-empty">Последний аудит не сообщил о тестах и покрытии.</p>
          ) : (
            <ul className="gate-failures">
              {testGates.map((g) => (
                <li key={g.gate}>
                  <div className="row">
                    <GateBadge result={g.result} />
                    <strong>{GATE_LABEL[g.gate]}</strong>
                  </div>
                  {g.reason && <p className="gate-reason">{g.reason}</p>}
                </li>
              ))}
            </ul>
          )}
          <p className="text-muted note">
            Подробности по отдельным тестам (наборы, длительность, упавшие тесты) API пока не отдаёт: видно решение gate и его причину. Тесты и mypy
            ставят зависимости проекта, поэтому без доступа песочницы к PyPI или npm (allowlist в .ai-coding.yml) они пропускаются.
          </p>
        </Card>
      </div>
    </div>
  );
}

export default function Testing() {
  const current = useUi((s) => s.projectId);
  const projects = useProjects();
  const [project, setProject] = useState(current ?? "");
  useEffect(() => {
    if (!project && projects.data?.[0]) setProject(current ?? projects.data[0].id);
  }, [project, current, projects.data]);
  const audits = useRuns({ kind: "audit" });
  const projectAudits = (audits.data ?? []).filter((r) => r.project_id === project);
  return (
    <div className="page">
      <PageHeader
        icon={<FlaskConical size={18} />}
        title="Тесты и Quality Gates"
        subtitle="Решения gates сборки, тестов, статического анализа, безопасности, покрытия и ревью — по последним аудитам и в истории."
        actions={
          <Select
            label="Проект"
            value={project}
            onChange={setProject}
            options={[{ value: "", label: "—" }, ...(projects.data ?? []).map((p) => ({ value: p.id, label: p.name }))]}
          />
        }
      />
      <div className="stack">
        <Card title="Последний аудит каждого проекта" icon={<Gauge size={15} />}>
          <QueryState query={audits} skeleton={<Skeleton lines={3} />}>
            {(list) => <LatestPerProject audits={list} />}
          </QueryState>
        </Card>
        {project ? (
          <QueryState query={audits} skeleton={<Skeleton lines={5} />}>
            {() => <History_ runs={projectAudits} />}
          </QueryState>
        ) : (
          <EmptyState title="Выберите проект">История gates показывается по одному проекту.</EmptyState>
        )}
      </div>
    </div>
  );
}
