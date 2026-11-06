import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Ban, Bot, Boxes, ChevronLeft, ChevronRight, FileWarning, Globe, KeyRound, Layers, ListChecks, Play, Scale, ShieldAlert, Sparkles, SquareCode } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import {
  type AgentVerdict,
  ApiError,
  api,
  TERMINAL,
  type CoverageCategory,
  type CoverageState,
  type Me,
  type SecurityAgentState,
  type SecurityCategory,
  type SecurityFinding,
  type SecurityFindingDetail,
  type SecurityOverview,
  type ToolState,
} from "../api";
import { queryClient, useProjects } from "../data/queries";
import { featureOn, href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Button,
  Card,
  type Column,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  LinkButton,
  PageHeader,
  relativeTime,
  Select,
  SEVERITIES,
  SeverityBadge,
  Skeleton,
  Stat,
  StateBadge,
  type Tone,
  toast,
} from "../ui";
import "./quality.css";
import "./security.css";

// AI Security Center (opt-in): what the platform's analysers and the Security Agent
// covered in one run and what they found. Execution is not a verdict: a completed scan
// can hold many findings, and no findings never means "secure". Every string below comes
// from scanners, the scanned project or a model and is rendered as plain text. Starting a
// security audit goes through the ordinary runs API (rights, approvals, Quality Gates).

export const PAGE_SIZE = 50;
const LIVE = 5_000;

/** The scanners of a security audit (the server's profile, GET … → launch.tools). */
export const SECURITY_TOOLS = ["semgrep", "osv-scanner", "trivy", "gitleaks"];
const TOOL_ROLE: Record<string, string> = {
  semgrep: "SAST: опасные конструкции в коде",
  "osv-scanner": "SCA: уязвимые зависимости по базе OSV",
  trivy: "SCA и IaC: уязвимые пакеты и ошибки конфигурации",
  gitleaks: "секреты в репозитории",
};
/** Project roles that may start runs (the server checks it again). */
const RUN_ROLES = new Set(["owner", "maintainer", "developer"]);

export const VERDICTS: Record<AgentVerdict, { label: string; tone: Tone }> = {
  confirmed: { label: "подтверждена", tone: "danger" },
  false_positive: { label: "вероятно ложная", tone: "neutral" },
  needs_review: { label: "нужна проверка", tone: "warning" },
};

const AGENT_STATE: Record<SecurityAgentState["state"], { label: string; tone: Tone }> = {
  not_requested: { label: "Не запрашивалась", tone: "neutral" },
  awaiting_approval: { label: "Ждёт утверждения", tone: "warning" },
  running: { label: "Проверяет", tone: "info" },
  completed: { label: "Готово", tone: "info" },
  skipped: { label: "Пропущено", tone: "warning" },
  error: { label: "Ошибка", tone: "danger" },
  not_run: { label: "Не выполнялась", tone: "warning" },
};

export const AGENT_REASONS: Record<string, string> = {
  ai_disabled: "AI выключен в настройках проекта — подключите AI-провайдер.",
  ai_key_missing: "Не задан ключ AI-провайдера проекта.",
  ai_unavailable: "Нет доступной модели (провайдер или политика проекта).",
  agent_failed: "Агент не смог выполнить проверку; аудит и Quality Gates при этом действительны.",
  task_failed: "Шаг агента завершился ошибкой.",
  task_skipped: "Шаг агента пропущен.",
  no_new_findings: "Новых находок для проверки нет.",
  cancelled: "Run отменён до проверки.",
};

const CATEGORIES: { id: SecurityCategory; label: string; icon: ReactNode; about: string }[] = [
  { id: "sast", label: "SAST", icon: <SquareCode size={15} />, about: "Статический анализ кода (Semgrep)." },
  { id: "sca", label: "SCA — зависимости", icon: <Boxes size={15} />, about: "Уязвимые зависимости: нужны оба анализатора, OSV-Scanner и Trivy." },
  { id: "secrets", label: "Секреты", icon: <KeyRound size={15} />, about: "Секреты в репозитории (Gitleaks). Значения секретов не показываются." },
  { id: "iac", label: "IaC", icon: <Layers size={15} />, about: "Misconfiguration-проверки Trivy fs. Это не доказательство, что проверены все IaC-ресурсы." },
  { id: "container", label: "Контейнеры", icon: <FileWarning size={15} />, about: "Сканирование образов контейнеров ещё не реализовано." },
  { id: "dast", label: "DAST", icon: <Globe size={15} />, about: "Динамическое сканирование ещё не реализовано: запросов к приложениям платформа не делает." },
];

export const COVERAGE: Record<CoverageState, { label: string; tone: Tone }> = {
  completed: { label: "Выполнено", tone: "info" },
  incomplete: { label: "Неполное", tone: "warning" },
  skipped: { label: "Пропущено", tone: "warning" },
  error: { label: "Ошибка", tone: "danger" },
  unsupported: { label: "Не реализовано", tone: "neutral" },
};

const TOOL_STATE: Record<ToolState, string> = {
  completed: "выполнен",
  skipped: "пропущен",
  error: "ошибка",
  running: "выполняется",
  not_run: "не запускался",
};

export const REASONS: Record<string, string> = {
  timeout: "превышено время выполнения",
  offline_db_missing: "нет офлайн-базы уязвимостей",
  output_too_large: "вывод превысил лимит",
  parse_error: "не удалось разобрать вывод",
  docker_only: "работает только в Docker-песочнице",
  tool_unavailable: "инструмент недоступен",
  tool_failed: "инструмент завершился с ошибкой",
  task_failed: "задача run'а упала",
  task_skipped: "задача run'а пропущена",
  cancelled: "отменено",
  not_planned: "не входил в этот run",
  not_applicable: "не применим к проекту",
  invalid_result: "некорректный результат",
  other: "другая причина",
};

const FINDING_CATEGORY: Record<SecurityFinding["category"], string> = {
  sast: "SAST",
  sca: "SCA",
  secrets: "Секреты",
  iac: "IaC",
  other: "Прочее",
};

const DETAIL_LABELS: [keyof SecurityFindingDetail["details"], string][] = [
  ["package", "Пакет"],
  ["installed_version", "Установленная версия"],
  ["fixed_version", "Исправлено в"],
  ["ecosystem", "Экосистема"],
  ["resolution", "Рекомендация сканера"],
];

/** Only a plain https reference becomes a link; anything else is not shown at all. */
export function safeReference(url: string | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password ? parsed.href : null;
  } catch {
    return null;
  }
}

function location(f: SecurityFinding): string {
  if (!f.file_path) return "—";
  return f.line ? `${f.file_path}:${f.line}` : f.file_path;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return "Run не найден, у вас нет доступа к проекту или модуль выключен на сервере.";
  if (error instanceof ApiError && error.status === 403) return "Недостаточно прав для просмотра находок этого проекта.";
  return error instanceof Error ? error.message : String(error);
}

// --------------------------------------------------------------- coverage --
function CoverageCard({ id, label, icon, about, data }: { id: SecurityCategory; label: string; icon: ReactNode; about: string; data: CoverageCategory | undefined }) {
  const state = data?.state ?? "incomplete";
  const info = COVERAGE[state];
  return (
    <Card title={label} icon={icon} actions={<Badge tone={info.tone} dot>{info.label}</Badge>} className={`sc-coverage sc-${state}`}>
      <p className="text-secondary sc-about">{about}</p>
      {data && data.tools.length > 0 ? (
        <ul className="sc-tools" aria-label={`Анализаторы: ${label}`}>
          {data.tools.map((t) => (
            <li key={`${id}-${t.tool}`}>
              <span className="mono">{t.tool}</span>
              <span className={`sc-tool-state sc-tool-${t.state}`}>{TOOL_STATE[t.state] ?? t.state}</span>
              {t.reason && <span className="text-muted">· {REASONS[t.reason] ?? REASONS.other}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-muted sc-about">Анализатора в платформе нет — эта категория не проверялась.</p>
      )}
    </Card>
  );
}

function Assurance({ data }: { data: SecurityOverview }) {
  const gate = data.security_gate;
  return (
    <div className="bento sc-assurance">
      <Card title="Quality Gate «security»" icon={<ListChecks size={15} />} span={2}>
        {gate ? (
          <p>
            <Badge tone={gate.result === "PASS" ? "success" : gate.result === "FAIL" ? "danger" : "warning"} dot>
              {gate.result}
            </Badge>{" "}
            {gate.blocking ? "блокирующий" : "не блокирующий"} · причина и доказательства — на{" "}
            <a href={href("run", data.run_id)}>странице run'а</a>
          </p>
        ) : (
          <p className="text-muted">Gate в этом run'е не вычислялся. Выполнение сканеров само по себе не означает PASS.</p>
        )}
      </Card>
      <Card title="Compliance" icon={<Scale size={15} />}>
        <p>
          <Badge>не оценивалось</Badge>
        </p>
        <p className="text-muted sc-about">Соответствие стандартам (OWASP ASVS и др.) не оценивается и не подтверждается.</p>
      </Card>
      <Card title="AI Fix" icon={<Bot size={15} />}>
        <p>
          <Badge>
            <Ban size={11} aria-hidden /> выключено
          </Badge>
        </p>
        <p className="text-muted sc-about">Исправления не генерируются и не применяются автоматически.</p>
      </Card>
    </div>
  );
}

// ------------------------------------------------------------ security agent --
function AgentCard({ data }: { data: SecurityOverview }) {
  const agent: SecurityAgentState = data.security_agent ?? { state: "not_requested", reason: null };
  const info = AGENT_STATE[agent.state] ?? AGENT_STATE.not_run;
  const risks = agent.additional_risks ?? [];
  return (
    <Card title="Security Agent — AI-проверка находок" icon={<Sparkles size={15} />} actions={<Badge tone={info.tone} dot>{info.label}</Badge>} className="sc-agent">
      {agent.state === "not_requested" && <p className="text-muted sc-about">В этом run'е AI-проверка не запрашивалась. Её можно включить при запуске аудита безопасности.</p>}
      {agent.state === "awaiting_approval" && (
        <p className="sc-about">
          Отправка находок и фрагментов кода во внешнюю модель ждёт утверждения. Утвердить или отклонить можно на <a href={href("run", data.run_id)}>странице run'а</a>.
        </p>
      )}
      {agent.state === "running" && <p className="text-muted sc-about">Агент проверяет находки сканеров…</p>}
      {agent.reason && agent.state !== "completed" && <p className="text-muted sc-about">{AGENT_REASONS[agent.reason] ?? AGENT_REASONS.agent_failed}</p>}
      {agent.state === "completed" && (
        <>
          <div className="stats">
            {(Object.keys(VERDICTS) as AgentVerdict[]).map((v) => (
              <Stat key={v} label={VERDICTS[v].label} value={agent.verdicts?.[v] ?? 0} tone={v === "confirmed" ? "danger" : v === "needs_review" ? "warning" : undefined} />
            ))}
            <Stat label="доп. риски" value={risks.length} />
          </div>
          <p className="text-muted sc-about">
            Проверено {agent.triaged ?? 0} из {agent.total ?? "?"} находок{agent.models && agent.models.length > 0 ? ` · ${agent.models.join(", ")}` : ""}. Вердикты — рекомендации модели: Quality Gates они не меняют, ложное срабатывание снимает только человек через baseline.
          </p>
          {agent.summary && <p className="sc-agent-text">{agent.summary}</p>}
          {risks.length > 0 && (
            <section aria-label="Дополнительные риски">
              <h4 className="section-title">Дополнительные риски, найденные агентом</h4>
              <ul className="sc-risks">
                {risks.map((r, i) => (
                  <li key={`${r.file}-${r.line}-${i}`}>
                    <SeverityBadge severity={r.severity} /> <span className="path">{r.file ? (r.line ? `${r.file}:${r.line}` : r.file) : "—"}</span>
                    {r.description && <p className="sc-agent-text">{r.description}</p>}
                    {r.recommendation && <p className="sc-agent-text text-secondary">Рекомендация: {r.recommendation}</p>}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </Card>
  );
}

// ----------------------------------------------------------------- launch --
function LaunchAudit({ projectId, role, onStarted }: { projectId: string; role: string | null | undefined; onStarted: (runId: string) => void }) {
  const [open, setOpen] = useState(false);
  const [triage, setTriage] = useState(false);
  const [busy, setBusy] = useState(false);
  const allowed = RUN_ROLES.has(role ?? "");
  function start() {
    setBusy(true);
    api.createRun(projectId, "audit", { tools: SECURITY_TOOLS, triage }).then(
      (run) => {
        setBusy(false);
        setOpen(false);
        void queryClient.invalidateQueries({ queryKey: ["security-runs", projectId] });
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        toast({ tone: "success", title: "Аудит безопасности запущен", text: triage ? "Сканеры и Security Agent" : "Сканеры безопасности" });
        setTriage(false);
        onStarted(run.id);
      },
      (err: unknown) => {
        setBusy(false);
        toast({ tone: "danger", title: "Аудит не запущен", text: errorMessage(err) });
      },
    );
  }
  return (
    <>
      <Button variant="primary" icon={<Play size={14} />} disabled={!allowed} title={allowed ? undefined : "Запускать аудит могут owner, maintainer и developer проекта"} onClick={() => setOpen(true)}>
        Запустить аудит безопасности
      </Button>
      {open && (
        <Dialog
          open
          onOpenChange={(o) => !o && !busy && setOpen(false)}
          title="Аудит безопасности"
          footer={
            <>
              <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
                Отмена
              </Button>
              <Button variant="primary" icon={<Play size={14} />} onClick={start} disabled={busy}>
                {busy ? "Запуск…" : "Запустить"}
              </Button>
            </>
          }
        >
          <div className="sc-launch">
            <p className="sc-about">Run вида audit в Docker-песочнице: код только читается, затем Quality Gates и отчёт — как в Audit Center. Проверяют:</p>
            <ul className="sc-launch-agents">
              {SECURITY_TOOLS.map((t) => (
                <li key={t}>
                  <span className="mono">{t}</span> — {TOOL_ROLE[t]}
                </li>
              ))}
            </ul>
            <label className="check">
              <input type="checkbox" checked={triage} onChange={(e) => setTriage(e.target.checked)} /> AI-проверка: Security Agent оценит каждую находку
            </label>
            <p className="text-muted sc-about">
              Агент получает находки и фрагменты затронутых файлов (файлы с секретами не отправляются), подтверждает или отклоняет находки, описывает сценарий атаки и исправление. Нужен AI-провайдер
              проекта; для внешнего провайдера run остановится и будет ждать утверждения на странице run'а. Вердикты агента — рекомендации, Quality Gates они не меняют.
            </p>
          </div>
        </Dialog>
      )}
    </>
  );
}

// ---------------------------------------------------------------- details --
function FindingDetails({ projectId, runId, finding, onClose }: { projectId: string; runId: string; finding: SecurityFinding; onClose: () => void }) {
  const detail = useQuery({
    queryKey: ["security-finding", projectId, runId, finding.id],
    queryFn: () => api.securityFinding(projectId, runId, finding.id),
  });
  const facts = detail.data?.details ?? {};
  const reference = safeReference(facts.reference);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()} title={finding.title} wide footer={<LinkButton href={href("run", runId)}>Открыть run</LinkButton>}>
      <div className="finding-detail">
        <dl className="kv">
          <dt>Важность</dt>
          <dd>
            <SeverityBadge severity={finding.severity} />
          </dd>
          <dt>Категория</dt>
          <dd>{FINDING_CATEGORY[finding.category] ?? finding.category}</dd>
          <dt>Инструмент</dt>
          <dd>{finding.source}</dd>
          <dt>Правило</dt>
          <dd className="mono">{finding.rule_id ?? "—"}</dd>
          <dt>Файл</dt>
          <dd className="path">{location(finding)}</dd>
          <dt>Статус</dt>
          <dd>{finding.status}</dd>
          <dt>Fingerprint</dt>
          <dd className="mono truncate">{finding.fingerprint}</dd>
          {DETAIL_LABELS.filter(([key]) => facts[key]).map(([key, label]) => (
            <FragmentRow key={key} label={label} value={String(facts[key])} />
          ))}
          {facts.aliases && facts.aliases.length > 0 && <FragmentRow label="Идентификаторы" value={facts.aliases.join(", ")} />}
          {facts.cwe && facts.cwe.length > 0 && <FragmentRow label="CWE" value={facts.cwe.join(", ")} />}
          {reference && (
            <>
              <dt>Источник</dt>
              <dd>
                <a href={reference} target="_blank" rel="noopener noreferrer nofollow" className="path">
                  {reference}
                </a>
              </dd>
            </>
          )}
        </dl>
        {detail.data?.agent && (
          <section className="sc-assessment" aria-label="Оценка Security Agent">
            <h4 className="section-title">Оценка Security Agent</h4>
            <p>
              <Badge tone={VERDICTS[detail.data.agent.verdict]?.tone ?? "neutral"} dot>
                {VERDICTS[detail.data.agent.verdict]?.label ?? detail.data.agent.verdict}
              </Badge>
              {detail.data.agent.severity && (
                <>
                  {" "}· важность по мнению агента: <SeverityBadge severity={detail.data.agent.severity} />
                </>
              )}
              {detail.data.agent.cwe && <span className="mono"> · {detail.data.agent.cwe}</span>}
            </p>
            {detail.data.agent.exploit_scenario && (
              <>
                <h5>Сценарий атаки</h5>
                <p className="sc-agent-text">{detail.data.agent.exploit_scenario}</p>
              </>
            )}
            {detail.data.agent.recommendation && (
              <>
                <h5>Как исправить</h5>
                <p className="sc-agent-text">{detail.data.agent.recommendation}</p>
              </>
            )}
            <p className="text-muted sc-about">Это рекомендация модели: она не меняет находку и Quality Gates. Цитаты кода из ответа агента не показываются.</p>
          </section>
        )}
        {detail.isLoading && <Skeleton lines={2} />}
        {detail.isError && <ErrorState error={new Error(errorMessage(detail.error))} title="Детали не загрузились" onRetry={() => void detail.refetch()} />}
        <p className="text-muted sc-withheld" role="note">
          {finding.category === "secrets"
            ? "Значение секрета, совпадение и сообщение сканера не выдаются. Отзовите и замените секрет: удаления строки из кода недостаточно."
            : "Сообщение сканера, фрагменты кода и исходные доказательства не выдаются: они могут содержать секреты или недоверенный текст."}
        </p>
      </div>
    </Dialog>
  );
}

function FragmentRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

// ------------------------------------------------------------------ table --
function Findings({ data, filters, onFilter, offset, onOffset, onOpen, fetching }: {
  data: SecurityOverview;
  filters: Filters;
  onFilter: (f: Filters) => void;
  offset: number;
  onOffset: (n: number) => void;
  onOpen: (f: SecurityFinding) => void;
  fetching: boolean;
}) {
  const sources = Object.keys(data.summary.by_source).sort();
  const statuses = Object.keys(data.summary.by_status).sort();
  const { total } = data.page;
  const filtered = Boolean(filters.severity || filters.source || filters.status);
  const columns: Column<SecurityFinding>[] = [
    { id: "severity", header: "Важность", cell: (f) => <SeverityBadge severity={f.severity} />, width: "104px" },
    {
      id: "title",
      header: "Находка",
      cell: (f) => (
        <>
          <span className="cell-main">{f.title}</span>
          {f.rule_id && <span className="cell-sub mono">{f.rule_id}</span>}
        </>
      ),
    },
    { id: "category", header: "Категория", cell: (f) => <Badge>{FINDING_CATEGORY[f.category] ?? f.category}</Badge> },
    {
      id: "agent",
      header: "AI-агент",
      cell: (f) => (f.agent_verdict && VERDICTS[f.agent_verdict] ? <Badge tone={VERDICTS[f.agent_verdict].tone}>{VERDICTS[f.agent_verdict].label}</Badge> : <span className="text-muted">—</span>),
    },
    { id: "file", header: "Файл", cell: (f) => <span className="path">{location(f)}</span> },
    { id: "source", header: "Инструмент", cell: (f) => <span className="mono">{f.source}</span> },
    { id: "status", header: "Статус", cell: (f) => <Badge tone={f.status === "open" ? "warning" : "neutral"}>{f.status}</Badge> },
  ];
  const empty = filtered ? (
    <EmptyState title="Нет находок по выбранным фильтрам" action={<Button size="sm" variant="ghost" onClick={() => onFilter({})}>Сбросить фильтры</Button>} />
  ) : (
    <EmptyState title="В этом run'е находок нет">
      Это не означает, что проект безопасен: проверьте покрытие выше — пропущенные, незавершённые и нереализованные проверки ничего не доказывают.
    </EmptyState>
  );
  return (
    <Card title={`Находки · ${total}`} icon={<ShieldAlert size={15} />} span={4} className="sc-findings">
      <div className="audit-filters" role="group" aria-label="Фильтры находок">
        <Select label="Важность" value={filters.severity ?? ""} onChange={(v) => onFilter({ ...filters, severity: v || undefined })} options={[{ value: "", label: "Все" }, ...SEVERITIES.map((s) => ({ value: s, label: s }))]} />
        <Select label="Инструмент" value={filters.source ?? ""} onChange={(v) => onFilter({ ...filters, source: v || undefined })} options={[{ value: "", label: "Все" }, ...sources.map((s) => ({ value: s, label: s }))]} />
        <Select label="Статус" value={filters.status ?? ""} onChange={(v) => onFilter({ ...filters, status: v || undefined })} options={[{ value: "", label: "Все" }, ...statuses.map((s) => ({ value: s, label: s }))]} />
      </div>
      <DataTable label="Находки run'а" rows={data.findings} columns={columns} rowKey={(f) => f.id} onRowClick={onOpen} empty={empty} />
      {total > 0 && (
        <nav className="sc-pager" aria-label="Страницы находок">
          <span className="text-secondary" aria-live="polite">
            {offset + 1}–{Math.min(offset + data.findings.length, total)} из {total}
            {fetching ? " · обновление…" : ""}
          </span>
          <Button size="sm" variant="ghost" icon={<ChevronLeft size={14} />} disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - PAGE_SIZE))}>
            Назад
          </Button>
          <Button size="sm" variant="ghost" icon={<ChevronRight size={14} />} disabled={offset + PAGE_SIZE >= total} onClick={() => onOffset(offset + PAGE_SIZE)}>
            Вперёд
          </Button>
        </nav>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------- page --
interface Filters {
  severity?: string;
  source?: string;
  status?: string;
}

function Overview({ projectId, runId }: { projectId: string; runId: string }) {
  const [filters, setFilters] = useState<Filters>({});
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<SecurityFinding | null>(null);
  useEffect(() => {
    setFilters({});
    setOffset(0);
    setOpen(null);
  }, [projectId, runId]);
  const overview = useQuery({
    queryKey: ["security-center", projectId, runId, filters, offset],
    queryFn: () => api.securityCenter(projectId, runId, { ...filters, limit: PAGE_SIZE, offset }),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data && !q.state.data.run.finished ? LIVE : false),
  });
  if (overview.isLoading) return <Skeleton lines={8} />;
  if (overview.isError || !overview.data) {
    return <ErrorState error={new Error(errorMessage(overview.error))} title="Обзор безопасности недоступен" onRetry={() => void overview.refetch()} />;
  }
  const data = overview.data;
  const s = data.summary;
  return (
    <>
      <div className="sc-notes">
        {!data.run.finished && (
          <p className="sc-note warning" role="status">
            Run ещё выполняется ({data.run.state}): покрытие и находки неполные, страница обновляется сама.
          </p>
        )}
        <p className="sc-note" role="note">
          Покрытие показывает, отработали ли анализаторы, а не результат проверки: «Выполнено» не означает PASS, отсутствие находок не означает безопасность.
        </p>
      </div>
      <section aria-label="Покрытие сканирования" className="bento sc-grid">
        {CATEGORIES.map((c) => (
          <CoverageCard key={c.id} {...c} data={data.coverage[c.id]} />
        ))}
      </section>
      <Assurance data={data} />
      <AgentCard data={data} />
      <Card title={`Находки run'а по важности · всего ${s.total}`} icon={<ShieldAlert size={15} />} className="sc-severity">
        <div className="stats">
          {SEVERITIES.map((level) => (
            <Stat key={level} label={level} value={s.by_severity[level] ?? 0} tone={level === "critical" || level === "high" ? "danger" : level === "medium" ? "warning" : undefined} />
          ))}
        </div>
        <p className="text-muted sc-about">Считаются все записи run'а, включая подавленные baseline и повторы; это не число уникальных уязвимостей.</p>
      </Card>
      <Findings
        data={data}
        filters={filters}
        onFilter={(f) => {
          setFilters(f);
          setOffset(0);
        }}
        offset={offset}
        onOffset={setOffset}
        onOpen={setOpen}
        fetching={overview.isFetching}
      />
      {open && <FindingDetails projectId={projectId} runId={runId} finding={open} onClose={() => setOpen(null)} />}
    </>
  );
}

function Header({ run, launch }: { run?: string; launch?: ReactNode }) {
  return (
    <PageHeader
      icon={<ShieldAlert size={20} />}
      title="Security Center"
      subtitle="Покрытие сканирования, находки и AI-проверка выбранного run'а. Права проверяются сервером."
      actions={
        run || launch ? (
          <>
            {run && <LinkButton href={href("run", run)}>Открыть run</LinkButton>}
            {launch}
          </>
        ) : undefined
      }
    />
  );
}

/** Off unless the server enables the module: then nothing is requested at all. */
export default function SecurityCenter({ me, query }: { me: Me; query: Record<string, string> }) {
  if (featureOn(me, "security_center")) return <Enabled query={query} />;
  return (
    <div className="page">
      <Header />
      <EmptyState icon={<Ban size={20} />} title="Модуль выключен на этом сервере">
        Security Center включает администратор сервера (AI_CODING_SERVER_SECURITY_CENTER=on). До этого его API и раздел недоступны.
      </EmptyState>
    </div>
  );
}

function Enabled({ query }: { query: Record<string, string> }) {
  const current = useUi((s) => s.projectId);
  const setCurrent = useUi((s) => s.setProject);
  const projects = useProjects();
  const [projectId, setProjectId] = useState<string>(query.project ?? "");
  const [runId, setRunId] = useState<string>(query.run ?? "");
  const project = projectId || current || projects.data?.[0]?.id || "";
  const runs = useQuery({
    queryKey: ["security-runs", project],
    queryFn: () => api.runs(project),
    enabled: Boolean(project),
    refetchInterval: (q) => (q.state.data?.some((r) => !TERMINAL.has(r.state)) ? LIVE : false),
  });
  const run = useMemo(() => (runs.data?.some((r) => r.id === runId) ? runId : runs.data?.[0]?.id ?? ""), [runs.data, runId]);
  const role = projects.data?.find((p) => p.id === project)?.role;
  const launch = project ? <LaunchAudit projectId={project} role={role} onStarted={setRunId} /> : undefined;
  return (
    <div className="page security-center">
      <Header run={run} launch={launch} />
      {projects.isError ? (
        <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
      ) : projects.isLoading ? (
        <Skeleton lines={2} />
      ) : (projects.data ?? []).length === 0 ? (
        <EmptyState title="Нет доступных проектов">Security Center показывает проекты, в которых у вас есть право просмотра находок.</EmptyState>
      ) : (
        <>
          <div className="audit-filters" role="group" aria-label="Проект и run">
            <Select
              label="Проект"
              value={project}
              onChange={(v) => {
                setProjectId(v);
                setCurrent(v);
                setRunId("");
              }}
              options={(projects.data ?? []).map((p) => ({ value: p.id, label: p.name }))}
            />
            {(runs.data ?? []).length > 0 && (
              <Select
                label="Run"
                value={run}
                onChange={setRunId}
                options={(runs.data ?? []).map((r) => ({ value: r.id, label: `${r.kind ?? r.mode.toLowerCase()} · ${r.state} · ${relativeTime(r.created_at)}` }))}
              />
            )}
            {run && runs.data && <StateBadge state={runs.data.find((r) => r.id === run)?.state ?? ""} />}
          </div>
          {runs.isError ? (
            <ErrorState error={new Error(errorMessage(runs.error))} title="Run'ы проекта не загрузились" onRetry={() => void runs.refetch()} />
          ) : runs.isLoading ? (
            <Skeleton lines={4} />
          ) : !run ? (
            <EmptyState title="У проекта ещё нет run'ов" action={launch}>
              Без run'а нечего показать — и это не означает, что проект безопасен. Запустите аудит безопасности.
            </EmptyState>
          ) : (
            <Overview projectId={project} runId={run} />
          )}
        </>
      )}
    </div>
  );
}
