import { Code2, History, ListChecks, Timer, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  codeHref,
  failedCount,
  isFailing,
  KIND_LABEL,
  type RunTests,
  type BriefCase,
  type TestCase,
  type TestRunSummary,
  type TestTotals,
  seconds,
  SETTING_HINT,
  slowest,
  STATUS_LABEL,
  useRunTests,
  useTestHistory,
  useTestRuns,
} from "../data/tests";
import { href } from "../shell/routes";
import { Badge, Card, EmptyState, QueryState, relativeTime, SearchInput, Segmented, shortId, Skeleton, type Tone } from "../ui";
import "./testing.css";

// «Тестовые прогоны»: per-test results of the project's runs (JUnit reports, ADR-0044) —
// totals per run, the suites with failed tests first, regressions against the previous
// run, the slowest tests, and links into Code Studio at the failing line. A run without
// results says why and names the setting behind it.

const TONE: Record<string, Tone> = { passed: "success", failed: "danger", error: "danger", skipped: "neutral", absent: "neutral" };

function StatusBadge({ status }: { status: string }) {
  return (
    <Badge tone={TONE[status] ?? "neutral"} dot>
      {STATUS_LABEL[status as keyof typeof STATUS_LABEL] ?? status}
    </Badge>
  );
}

/** Passed / failed / skipped as one stacked bar. */
function ResultBar({ totals }: { totals: TestTotals }) {
  const failed = failedCount(totals);
  const label = `${totals.passed} пройдено, ${failed} упало, ${totals.skipped} пропущено из ${totals.tests}`;
  if (totals.tests === 0) return null;
  const pct = (n: number) => `${(n / totals.tests) * 100}%`;
  return (
    <span className="result-bar" role="img" aria-label={label} title={label}>
      <span className="seg-pass" style={{ width: pct(totals.passed) }} />
      <span className="seg-fail" style={{ width: pct(failed) }} />
      <span className="seg-skip" style={{ width: pct(totals.skipped) }} />
    </span>
  );
}

function OpenInCode({ projectId, test }: { projectId: string; test: Pick<TestCase, "file" | "line"> }) {
  const link = codeHref(projectId, test);
  if (!link) return null;
  return (
    <a className="test-open" href={link} title={`${test.file}${test.line ? `:${test.line}` : ""}`}>
      <Code2 size={13} aria-hidden />
      Открыть в Code Studio
    </a>
  );
}

function Location({ test }: { test: Pick<TestCase, "file" | "line"> }) {
  if (!test.file) return null;
  return (
    <span className="path test-path">
      {test.file}
      {test.line ? `:${test.line}` : ""}
    </span>
  );
}

/** Why a run has no per-test results — honest, with the setting to change. */
function NoResults({ run }: { run: Pick<RunTests, "reason" | "setting" | "problems"> }) {
  return (
    <div className="tests-none" role="note">
      <TriangleAlert size={15} aria-hidden />
      <div>
        <p>
          <strong>Результатов по отдельным тестам нет.</strong> {run.reason ?? "Отчёт JUnit не найден."}
        </p>
        {run.setting && (
          <p className="text-muted">
            Настройка <code>{run.setting}</code>
            {SETTING_HINT[run.setting] ? `: ${SETTING_HINT[run.setting]}` : "."}
          </p>
        )}
      </div>
    </div>
  );
}

function RunRow({ run, selected, onSelect }: { run: TestRunSummary; selected: boolean; onSelect: () => void }) {
  const failed = failedCount(run.totals);
  return (
    <li>
      <button type="button" className={selected ? "test-run active" : "test-run"} aria-pressed={selected} onClick={onSelect}>
        <span className="test-run-head">
          <strong>{KIND_LABEL[run.kind ?? ""] ?? run.kind ?? "run"}</strong>
          <span className="text-muted">
            {relativeTime(run.created_at)} · {shortId(run.run_id)}
          </span>
        </span>
        {run.totals.tests > 0 ? (
          <span className="test-run-totals">
            <ResultBar totals={run.totals} />
            <span className="test-run-counts">
              <span className="ok">{run.totals.passed} ✓</span>
              {failed > 0 && <span className="bad">{failed} ✗</span>}
              {run.totals.skipped > 0 && <span className="text-muted">{run.totals.skipped} пропущено</span>}
              {run.regressions.length > 0 && <Badge tone="danger">регрессий: {run.regressions.length}</Badge>}
            </span>
          </span>
        ) : (
          <span className="text-muted test-run-reason">{run.problems[0] ?? run.reason ?? "нет отчёта"}</span>
        )}
      </button>
    </li>
  );
}

function CaseHistory({ projectId, testKey }: { projectId: string; testKey: string }) {
  const history = useTestHistory(projectId, testKey);
  return (
    <QueryState query={history} skeleton={<Skeleton lines={1} />}>
      {(points) =>
        points.length === 0 ? (
          <p className="text-muted">Истории нет.</p>
        ) : (
          <ol className="case-history" aria-label="История теста по прогонам, новые первыми">
            {points.map((p) => (
              <li key={p.run_id}>
                <a href={href("run", p.run_id)} title={p.message || undefined}>
                  <StatusBadge status={p.status} />
                  <span className="text-muted">{relativeTime(p.created_at)}</span>
                </a>
              </li>
            ))}
          </ol>
        )
      }
    </QueryState>
  );
}

function CaseRow({ projectId, test, regression }: { projectId: string; test: TestCase; regression: boolean }) {
  const [open, setOpen] = useState(false);
  const failing = isFailing(test.status);
  return (
    <li className={failing ? "test-case failing" : "test-case"}>
      <div className="test-case-head">
        <StatusBadge status={test.status} />
        <span className="test-name">
          {test.name}
          {test.classname && <span className="text-muted test-class"> · {test.classname}</span>}
        </span>
        {regression && <Badge tone="danger">регрессия</Badge>}
        <span className="test-time">{seconds(test.time)}</span>
      </div>
      {(failing || test.status === "skipped") && test.message && <p className="test-message">{test.message}</p>}
      <div className="test-case-foot">
        <Location test={test} />
        <OpenInCode projectId={projectId} test={test} />
        <button type="button" className="test-more" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Скрыть" : failing && test.details ? "Подробности и история" : "История"}
        </button>
      </div>
      {open && (
        <div className="test-details">
          {test.details && <pre aria-label="Вывод теста">{test.details}</pre>}
          <CaseHistory projectId={projectId} testKey={test.key} />
        </div>
      )}
    </li>
  );
}

function RunDetail({ projectId, runId }: { projectId: string; runId: string }) {
  const query = useRunTests(runId);
  const [filter, setFilter] = useState<"failing" | "all">("failing");
  const [search, setSearch] = useState("");
  return (
    <QueryState query={query} skeleton={<Skeleton lines={6} />}>
      {(run) => {
        const failed = failedCount(run.totals);
        const regressions = new Set(run.regressions.map((r) => r.key));
        const needle = search.trim().toLowerCase();
        const shown = run.suites
          .map((s) => ({
            ...s,
            cases: s.cases.filter(
              (c) => (filter === "all" || isFailing(c.status)) && (!needle || `${c.name} ${c.classname} ${c.file ?? ""}`.toLowerCase().includes(needle)),
            ),
          }))
          .filter((s) => s.cases.length > 0);
        return (
          <div className="stack">
            <div className="test-totals" aria-label="Итог прогона">
              <span>
                <strong>{run.totals.tests}</strong> тестов
              </span>
              <span className="ok">
                <strong>{run.totals.passed}</strong> пройдено
              </span>
              <span className={failed ? "bad" : undefined}>
                <strong>{failed}</strong> упало
              </span>
              <span>
                <strong>{run.totals.skipped}</strong> пропущено
              </span>
              <span>
                <Timer size={13} aria-hidden /> {seconds(run.totals.time)}
              </span>
              <a href={href("run", run.run_id)}>Открыть run</a>
            </div>
            {run.totals.tests === 0 && <NoResults run={run} />}
            {run.problems.length > 0 && (
              <ul className="tests-problems" aria-label="Отчёты, которые не удалось прочитать">
                {run.problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            )}
            {run.regressions.length > 0 && (
              <section className="regressions" aria-label="Регрессии">
                <h4>
                  Регрессии: прошли в прогоне {shortId(run.compared_with ?? "")}, падают теперь ({run.regressions.length})
                </h4>
                <ul>
                  {run.regressions.map((r: BriefCase) => (
                    <li key={r.key}>
                      <span className="test-name">{r.name}</span>
                      {r.message && <span className="text-muted test-message-inline">{r.message}</span>}
                      <OpenInCode projectId={projectId} test={r} />
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {run.fixed.length > 0 && (
              <p className="text-muted">
                Починено с прошлого прогона: {run.fixed.length} ({run.fixed.slice(0, 3).join(", ")}
                {run.fixed.length > 3 ? "…" : ""})
              </p>
            )}
            {run.totals.tests > 0 && (
              <>
                <div className="tests-filters">
                  <Segmented
                    label="Какие тесты показать"
                    value={filter}
                    onChange={setFilter}
                    options={[
                      { value: "failing", label: `Упавшие (${failed})` },
                      { value: "all", label: `Все (${run.totals.tests})` },
                    ]}
                  />
                  <SearchInput label="Найти тест" value={search} onChange={setSearch} placeholder="Имя, класс или файл" />
                </div>
                {shown.length === 0 ? (
                  <p className="text-muted">{filter === "failing" && !needle ? "Упавших тестов нет — все прошли или пропущены." : "Ничего не найдено."}</p>
                ) : (
                  shown.map((suite) => (
                    <details key={suite.name} className="test-suite" open={suite.failures + suite.errors > 0 || shown.length === 1}>
                      <summary>
                        <span className="suite-name">{suite.name}</span>
                        <span className="text-muted suite-meta">
                          {suite.tool} · {suite.tests} тестов
                          {suite.failures + suite.errors > 0 ? ` · упало ${suite.failures + suite.errors}` : ""}
                          {suite.skipped ? ` · пропущено ${suite.skipped}` : ""} · {seconds(suite.time)}
                        </span>
                      </summary>
                      <ul className="test-cases">
                        {suite.cases.map((c) => (
                          <CaseRow key={`${c.key}#${c.file ?? ""}`} projectId={projectId} test={c} regression={regressions.has(c.key)} />
                        ))}
                      </ul>
                    </details>
                  ))
                )}
              </>
            )}
            {run.reports.length > 0 && (
              <p className="text-muted tests-reports">
                Отчёты:{" "}
                {run.reports.map((r, i) => (
                  <span key={r.name}>
                    {i > 0 && ", "}
                    <code>{r.name}</code>
                    {!r.primary && r.totals ? ` (не в итоге: ${r.totals.passed}/${r.totals.tests} пройдено)` : ""}
                  </span>
                ))}
              </p>
            )}
          </div>
        );
      }}
    </QueryState>
  );
}

function Slowest({ projectId, runId }: { projectId: string; runId: string }) {
  const query = useRunTests(runId);
  const top = useMemo(() => slowest(query.data?.suites ?? []), [query.data]);
  if (!query.data || top.length === 0) return <p className="text-muted tile-empty">Длительности тестов нет в отчёте.</p>;
  const max = top[0]?.time || 1;
  return (
    <ol className="slowest">
      {top.map((c) => (
        <li key={`${c.suite}/${c.key}`}>
          <span className="slow-name" title={c.key}>
            {c.name}
          </span>
          <span className="slow-bar" aria-hidden>
            <span style={{ width: `${(c.time / max) * 100}%` }} />
          </span>
          <span className="test-time">{seconds(c.time)}</span>
          <OpenInCode projectId={projectId} test={c} />
        </li>
      ))}
    </ol>
  );
}

export function TestRuns({ projectId }: { projectId: string }) {
  const runs = useTestRuns(projectId);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => setSelected(null), [projectId]);
  const list = runs.data ?? [];
  const current = selected ?? list.find((r) => r.totals.tests > 0)?.run_id ?? list[0]?.run_id ?? null;
  return (
    <Card title="Тестовые прогоны" icon={<ListChecks size={15} />}>
      <QueryState
        query={runs}
        skeleton={<Skeleton lines={4} />}
        empty={(data) =>
          data.length === 0 ? (
            <EmptyState icon={<ListChecks size={20} />} title="Прогонов с тестами ещё нет">
              Тесты проекта запускаются в песочнице Docker в режимах «Разработка», «Модернизация» и «Сопровождение»; аудит запускает go test и cargo test. Каждый
              прогон оставляет отчёт JUnit — здесь появятся его наборы и тесты.
            </EmptyState>
          ) : null
        }
      >
        {(data) => (
          <div className="test-runs">
            <ul className="test-run-list" aria-label="Прогоны с тестами, новые первыми">
              {data.map((r) => (
                <RunRow key={r.run_id} run={r} selected={r.run_id === current} onSelect={() => setSelected(r.run_id)} />
              ))}
            </ul>
            {current && (
              <div className="test-run-detail">
                <RunDetail projectId={projectId} runId={current} />
                <h4 className="section-title">
                  <Timer size={13} aria-hidden /> Самые медленные тесты
                </h4>
                <Slowest projectId={projectId} runId={current} />
              </div>
            )}
          </div>
        )}
      </QueryState>
      <p className="text-muted note">
        <History size={12} aria-hidden /> Отчёты JUnit пишут pytest, node --test и Vitest (платформа добавляет нужные флаги сама); go test и cargo test
        переводятся в тот же формат. Jest без jest-junit и своя команда без отчёта в /deps/junit.xml дают только итог gate.
      </p>
    </Card>
  );
}
