import { BrainCircuit, Cloud, Coins, Cpu, KeyRound, Lock, PlugZap, RotateCcw, Save, Server, Wallet } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Bar, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";
import { type AgentExecution, ApiError, errorText } from "../api";
import { type AIBudgets, aiApi, aiKey, type AIProvider, type AISettings, type AIState, type AITestResult, refreshAI, useAI, useAIModels } from "../data/ai";
import { queryClient, useAgentExecutions, useProject, useUsage } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  EmptyState,
  formatCost,
  formatNumber,
  LinkButton,
  PageHeader,
  QueryState,
  relativeTime,
  Segmented,
  Select,
  Skeleton,
  Stat,
  toast,
} from "../ui";
import { SecretField, useSecrets } from "../ui/SecretField";
import "./quality.css";
import "./models.css";

// The orchestrator's AI provider for the current project, set from the web (ADR-0046):
// the web values override the project's .ai-coding.yml for server runs, field by field;
// the API key is the write-only project secret "ai.api_key" — saved through SecretField,
// never sent back by the server and never shown here. Owners and maintainers edit, the
// others see the same page read-only. Usage and per-agent models stay below.

const PROVIDERS: { id: AIProvider; name: string; kind: "cloud" | "local"; text: string }[] = [
  { id: "anthropic", name: "Anthropic", kind: "cloud", text: "Claude по API-ключу Anthropic Console" },
  { id: "openai", name: "OpenAI", kind: "cloud", text: "GPT по API-ключу OpenAI" },
  { id: "ollama", name: "Ollama", kind: "local", text: "Локальные модели на вашем сервере, без ключа" },
  { id: "vllm", name: "vLLM", kind: "local", text: "Свой OpenAI-совместимый сервер моделей" },
];
const NAMES: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", ollama: "Ollama", vllm: "vLLM", none: "не выбран" };
const isCloud = (p: string) => p === "openai" || p === "anthropic";
const OLLAMA_URL = "http://localhost:11434/v1";
const CUSTOM = "__custom__";
const KEY_SECRET = "ai.api_key";

const BUDGETS: { id: keyof AIBudgets; label: string; step: string }[] = [
  { id: "max_tokens_per_run", label: "Токенов на run", step: "1000" },
  { id: "max_cost_usd_per_run", label: "Стоимость на run, $", step: "0.5" },
  { id: "max_duration_minutes", label: "Минут на run", step: "5" },
];

const YAML = `ai:
  enabled: true
  provider: anthropic          # openai | anthropic | ollama | vllm
  model: <model-id>
  api_key_env: ANTHROPIC_API_KEY  # имя переменной окружения на сервере, не сам ключ

security:
  network: allowlist           # local-only запрещает внешние LLM (Private Mode)
  egress_allowlist: [api.anthropic.com]`;

// ------------------------------------------------------------------ the form --
/** The form's draft: strings as typed; an absent field inherits the file. */
interface Form {
  enabled?: boolean;
  provider?: AIProvider;
  model?: string;
  base_url?: string;
  routing?: Record<string, string>;
  fallback?: string;
  budgets?: Partial<Record<keyof AIBudgets, string>>;
  private?: boolean;
}

function toForm(s: AISettings): Form {
  const form: Form = { ...s, fallback: s.fallback?.join(", "), budgets: undefined };
  if (s.budgets) form.budgets = Object.fromEntries(Object.entries(s.budgets).map(([k, v]) => [k, String(v)]));
  return clean(form);
}

function clean<T extends object>(value: T): T {
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as T;
}

/** What is sent: empty text inherits the file again. */
function toSettings(f: Form): AISettings {
  const out: AISettings = {};
  if (f.enabled !== undefined) out.enabled = f.enabled;
  if (f.provider) out.provider = f.provider;
  if (f.model?.trim()) out.model = f.model.trim();
  if (f.base_url?.trim()) out.base_url = f.base_url.trim();
  if (f.routing) out.routing = Object.fromEntries(Object.entries(f.routing).filter(([, m]) => m.trim()).map(([a, m]) => [a, m.trim()]));
  if (f.fallback !== undefined) out.fallback = f.fallback.split(/[\s,]+/).filter(Boolean);
  if (f.budgets) {
    const budgets = Object.fromEntries(
      Object.entries(f.budgets)
        .filter(([, v]) => v !== undefined && v.trim() !== "" && Number.isFinite(Number(v)))
        .map(([k, v]) => [k, Number(v)]),
    );
    if (Object.keys(budgets).length) out.budgets = budgets;
  }
  if (f.private !== undefined) out.private = f.private;
  return out;
}

/** Order-independent JSON, to compare a draft with what is saved. */
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.keys(value)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stable((value as Record<string, unknown>)[k])}`)
      .join(",")}}`;
  return JSON.stringify(value);
}

function Source({ web, onReset, disabled }: { web: boolean; onReset: () => void; disabled: boolean }) {
  return web ? (
    <>
      <Badge tone="accent">задано в веб</Badge>
      {!disabled && (
        <button type="button" className="ai-reset" onClick={onReset}>
          вернуть из файла
        </button>
      )}
    </>
  ) : (
    <Badge>из .ai-coding.yml</Badge>
  );
}

function Field({ label, htmlFor, source, children, hint }: { label: string; htmlFor?: string; source?: ReactNode; children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="ai-field">
      <div className="ai-field-head">
        {htmlFor ? (
          <label className="field-label" htmlFor={htmlFor}>
            {label}
          </label>
        ) : (
          <span className="field-label">{label}</span>
        )}
        {source}
      </div>
      {children}
      {hint && <p className="hint text-muted">{hint}</p>}
    </div>
  );
}

function problems(form: Form, state: AIState, values: { provider: string; model: string; baseUrl: string; enabled: boolean; privateMode: boolean }): string[] {
  const out: string[] = [];
  const switched = form.provider !== undefined && form.provider !== state.file.provider;
  if (values.enabled && values.provider === "none") out.push("Выберите провайдера.");
  if (switched && !values.model.trim()) out.push(`Укажите модель для ${NAMES[values.provider]}.`);
  if (values.provider === "vllm" && !values.baseUrl.trim()) out.push("Для vLLM нужен адрес сервера моделей.");
  if (values.privateMode && isCloud(values.provider))
    out.push(`Private Mode запрещает облачного провайдера ${NAMES[values.provider]} — выберите Ollama или vLLM либо выключите Private Mode.`);
  return out;
}

function Probe({ projectId, state, dirty }: { projectId: string; state: AIState; dirty: boolean }) {
  const [result, setResult] = useState<AITestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const effective = state.effective;
  const disabled = !state.can_edit || dirty || effective.provider === "none" || busy;
  async function check() {
    setBusy(true);
    try {
      setResult(await aiApi.test(projectId));
    } catch (err) {
      setResult({ ok: false, provider: effective.provider, model: effective.model, latency_ms: 0, models_offered: 0, detail: errorText(err) });
    } finally {
      setBusy(false);
      void queryClient.invalidateQueries({ queryKey: ["ai-models", projectId] });
    }
  }
  return (
    <div className="ai-probe">
      <div className="row">
        <Button icon={<PlugZap size={14} />} loading={busy} disabled={disabled} onClick={() => void check()}>
          Проверить подключение
        </Button>
      </div>
      <p className="hint text-muted detail">
        {!state.can_edit
          ? "Проверять подключение могут владелец и мейнтейнер проекта."
          : effective.provider === "none"
            ? "Сначала выберите и сохраните провайдера."
            : dirty
              ? "Проверяются сохранённые настройки — сначала сохраните изменения."
              : `Сервер обратится к ${NAMES[effective.provider]} со списком моделей; политика сети проекта действует и здесь.`}
      </p>
      <div role="status" aria-live="polite">
        {result &&
          (result.ok ? (
            <div className="stack">
              <Badge tone="success" dot>
                Подключение работает
              </Badge>
              <span className="text-secondary detail">
                {NAMES[result.provider] ?? result.provider}
                {result.model ? ` · ${result.model}` : ""} · {result.latency_ms} мс · моделей у провайдера: {result.models_offered}
              </span>
            </div>
          ) : (
            <div className="stack">
              <Badge tone="danger" dot>
                Не удалось подключиться
              </Badge>
              <p className="mono detail">{result.detail}</p>
            </div>
          ))}
      </div>
    </div>
  );
}

function KeyState({ state }: { state: AIState }) {
  const key = state.key;
  if (key.source === "env")
    return (
      <span className="text-secondary detail">
        {key.set ? <Badge tone="success">ключ задан</Badge> : <Badge tone="warning">переменная не задана</Badge>} переменная окружения сервера <code>{key.env}</code> из
        .ai-coding.yml
      </span>
    );
  return key.set ? (
    <Badge tone="success">
      <KeyRound size={11} aria-hidden /> ключ задан в веб
    </Badge>
  ) : (
    <Badge tone="warning">ключ не задан</Badge>
  );
}

function AIEditor({ projectId, state }: { projectId: string; state: AIState }) {
  const [form, setForm] = useState<Form>(() => toForm(state.settings));
  const [custom, setCustom] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  // A new version (saved here or elsewhere): the draft starts again from it. The editor
  // itself stays mounted — a key being typed into SecretField is not lost.
  const [version, setVersion] = useState(state.version);
  if (version !== state.version) {
    setVersion(state.version);
    setForm(toForm(state.settings));
    setCustom(false);
  }
  const readOnly = !state.can_edit;
  const file = state.file;

  // A key saved or removed through SecretField changes what the server reports.
  const secrets = useSecrets(projectId, state.can_edit);
  const seen = useRef<number | null>(null);
  useEffect(() => {
    if (!secrets.dataUpdatedAt) return;
    if (seen.current !== null && seen.current !== secrets.dataUpdatedAt) void refreshAI(projectId);
    seen.current = secrets.dataUpdatedAt;
  }, [secrets.dataUpdatedAt, projectId]);

  const switched = form.provider !== undefined && form.provider !== file.provider;
  const provider: AIProvider | "none" = form.provider ?? file.provider;
  const enabled = form.enabled ?? file.enabled;
  const model = form.model ?? (switched ? "" : (file.model ?? ""));
  const baseUrl = form.base_url ?? (switched ? "" : (file.base_url ?? ""));
  const routing = form.routing ?? (switched ? {} : file.routing);
  const fallback = form.fallback ?? (switched ? "" : file.fallback.join(", "));
  const privateMode = form.private ?? file.private;
  const settings = useMemo(() => toSettings(form), [form]);
  const dirty = stable(settings) !== stable(state.settings);
  const issues = problems(form, state, { provider, model, baseUrl, enabled, privateMode });

  const models = useAIModels(projectId, state);
  const sameTarget = provider === state.effective.provider && baseUrl === (state.effective.base_url ?? "");
  const offered = sameTarget ? (models.data?.models ?? []) : [];
  const pick = offered.length > 0 && !custom && (!model || offered.includes(model));

  function patch<K extends keyof Form>(field: K, value: Form[K] | undefined) {
    setServerError(null);
    setForm((f) => clean({ ...f, [field]: value }));
  }

  function choose(id: AIProvider | undefined) {
    setCustom(false);
    setServerError(null);
    // The file's provider again: inherit it (with its endpoint, key variable and routes).
    const inherit = id === undefined || id === file.provider;
    setForm((f) =>
      clean({ ...f, provider: inherit ? undefined : id, model: undefined, base_url: !inherit && id === "ollama" ? OLLAMA_URL : undefined, routing: undefined, fallback: undefined }),
    );
  }

  async function save(next: AISettings, done: string) {
    setSaving(true);
    setServerError(null);
    try {
      const saved = await aiApi.put(projectId, next, state.version);
      queryClient.setQueryData(aiKey(projectId), saved);
      void refreshAI(projectId);
      toast({ tone: "success", title: done, text: "Новые запуски оркестратора используют эти настройки." });
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        toast({ tone: "warning", title: "Настройки уже изменил кто-то другой", text: "Показаны актуальные значения — внесите изменения ещё раз." });
        void refreshAI(projectId);
      } else setServerError(errorText(err));
    } finally {
      setSaving(false);
    }
  }

  function resetAll() {
    if (window.confirm("Убрать все настройки ИИ, заданные в веб? Будет действовать .ai-coding.yml проекта. Ключ из веба останется — удалите его отдельно.")) void save({}, "Действует .ai-coding.yml");
  }

  const modelOptions = [
    ...(model && !offered.includes(model) ? [] : [{ value: "", label: "— выберите модель —" }]),
    ...offered.map((m) => ({ value: m, label: m })),
    { value: CUSTOM, label: "Другая модель…" },
  ];

  return (
    <>
      {!state.valid && state.error && (
        <p className="ai-banner danger" role="alert">
          Настройки из веба не подходят к .ai-coding.yml проекта — пока их не исправят, действует файл: {state.error}
        </p>
      )}
      {readOnly && <p className="ai-banner">Только просмотр: настройки ИИ меняют владелец и мейнтейнер проекта.</p>}
      <div className="bento">
        <Summary state={state} />
        <Card span={2} title="Проверка подключения" icon={<PlugZap size={15} />}>
          <Probe projectId={projectId} state={state} dirty={dirty} />
        </Card>
      </div>
      <Card title="Провайдер агентов оркестратора" icon={<BrainCircuit size={15} />}>
        <div className="ai-form">
          <Field label="ИИ агентов" source={<Source web={form.enabled !== undefined} disabled={readOnly} onReset={() => patch("enabled", undefined)} />}
            hint={enabled ? undefined : "Без ИИ работают только детерминированные проверки: линтеры, тесты, сканеры."}>
            <label className="check">
              <input type="checkbox" role="switch" checked={enabled} disabled={readOnly} onChange={(e) => patch("enabled", e.target.checked)} /> Включён для
              агентов этого проекта
            </label>
          </Field>

          <Field label="Провайдер" source={<Source web={form.provider !== undefined} disabled={readOnly} onReset={() => choose(undefined)} />}>
            <div className="provider-grid" role="radiogroup" aria-label="Провайдер">
              {PROVIDERS.map((p) => {
                const current = provider === p.id;
                const forbidden = privateMode && p.kind === "cloud";
                return (
                  <button
                    key={p.id}
                    type="button"
                    role="radio"
                    aria-checked={current}
                    className={current ? "provider-card current" : "provider-card"}
                    disabled={readOnly || (forbidden && !current)}
                    onClick={() => choose(p.id)}
                  >
                    <span className="row">
                      {p.kind === "cloud" ? <Cloud size={16} aria-hidden /> : <Server size={16} aria-hidden />}
                      <strong>{p.name}</strong>
                      <span className="card-spacer" />
                      {current ? <Badge tone={enabled ? "success" : "neutral"}>{enabled ? "выбран" : "выбран, ИИ выключен"}</Badge> : forbidden ? <Badge tone="danger">запрещён Private Mode</Badge> : null}
                    </span>
                    <small className="text-muted">{p.text}</small>
                  </button>
                );
              })}
            </div>
          </Field>

          {provider !== "none" && (
            <Field
              label="Модель"
              htmlFor={pick ? undefined : "ai-model"}
              source={<Source web={form.model !== undefined} disabled={readOnly} onReset={() => patch("model", undefined)} />}
              hint={
                !sameTarget
                  ? "Список моделей появится после сохранения провайдера."
                  : models.data?.reason && !offered.length && state.can_edit
                    ? `Список моделей недоступен: ${models.data.reason}`
                    : undefined
              }
            >
              {pick ? (
                <Select
                  label="Модель"
                  hideLabel
                  value={model}
                  options={modelOptions}
                  onChange={(v) => (v === CUSTOM ? setCustom(true) : patch("model", v || undefined))}
                />
              ) : (
                <input
                  id="ai-model"
                  className="input mono"
                  value={model}
                  disabled={readOnly}
                  spellCheck={false}
                  placeholder={provider === "ollama" ? "qwen2.5-coder:7b" : provider === "anthropic" ? "claude-…" : provider === "openai" ? "gpt-…" : "имя модели на сервере"}
                  onChange={(e) => patch("model", e.target.value)}
                />
              )}
            </Field>
          )}

          {(provider === "ollama" || provider === "vllm") && (
            <Field
              label="Адрес сервера моделей"
              htmlFor="ai-base-url"
              source={<Source web={form.base_url !== undefined} disabled={readOnly} onReset={() => patch("base_url", undefined)} />}
              hint="OpenAI-совместимый API, например http://10.0.0.5:8000/v1. Удалённый адрес — только https; адреса облачных метаданных сервер не вызывает."
            >
              <input
                id="ai-base-url"
                className="input mono"
                type="url"
                value={baseUrl}
                disabled={readOnly}
                spellCheck={false}
                placeholder={provider === "ollama" ? OLLAMA_URL : "https://models.example.org/v1"}
                onChange={(e) => patch("base_url", e.target.value)}
              />
            </Field>
          )}

          {isCloud(provider) && (
            <details className="ai-field">
              <summary className="field-label">Другой адрес API (прокси или шлюз)</summary>
              <input
                id="ai-base-url"
                aria-label="Адрес API"
                className="input mono"
                type="url"
                value={baseUrl}
                disabled={readOnly}
                spellCheck={false}
                placeholder={provider === "openai" ? "https://api.openai.com/v1" : "https://api.anthropic.com"}
                onChange={(e) => patch("base_url", e.target.value)}
              />
            </details>
          )}

          {provider !== "none" && provider !== "ollama" && (
            <Field label="API-ключ" source={<KeyState state={state} />}>
              {state.can_edit ? (
                <SecretField
                  projectId={projectId}
                  name={KEY_SECRET}
                  label={`API-ключ ${NAMES[provider]}`}
                  placeholder={provider === "anthropic" ? "sk-ant-…" : provider === "openai" ? "sk-…" : "ключ сервера моделей, если нужен"}
                  help="Хранится зашифрованным на сервере и уходит только провайдеру: в ответах API, журналах, параметрах и артефактах запусков его нет."
                />
              ) : (
                <p className="hint text-muted">Ключ задают владелец и мейнтейнер проекта; его значение не показывается никому.</p>
              )}
            </Field>
          )}

          <Field
            label="Private Mode"
            source={<Source web={form.private !== undefined} disabled={readOnly} onReset={() => patch("private", undefined)} />}
            hint={
              privateMode
                ? "Код не покидает сервер: агентам доступны только локальные Ollama и vLLM, любой выход в сеть запрещён (security.network: local-only)."
                : "Выключен: разрешается только адрес выбранного провайдера — остальная политика сети проекта не меняется."
            }
          >
            <label className="check">
              <input type="checkbox" role="switch" checked={privateMode} disabled={readOnly} onChange={(e) => patch("private", e.target.checked)} />
              <Lock size={13} aria-hidden /> Только локальные модели
            </label>
          </Field>
        </div>
      </Card>

      <Card title="Модели по агентам и бюджеты" icon={<Wallet size={15} />}>
        <div className="ai-form">
          <Field
            label="Модель для агента"
            source={<Source web={form.routing !== undefined} disabled={readOnly} onReset={() => patch("routing", undefined)} />}
            hint={`Пусто — модель по умолчанию${model ? ` (${model})` : ""}. Модели того же провайдера.`}
          >
            <ul className="ai-routes" aria-label="Модели по агентам">
              {state.agents.map((agent) => (
                <li key={agent}>
                  <label className="mono" htmlFor={`ai-route-${agent}`}>
                    {agent}
                  </label>
                  <input
                    id={`ai-route-${agent}`}
                    className="input mono"
                    value={routing[agent] ?? ""}
                    disabled={readOnly || provider === "none"}
                    spellCheck={false}
                    placeholder={model || "по умолчанию"}
                    onChange={(e) => {
                      const next = { ...routing, [agent]: e.target.value };
                      if (!e.target.value) delete next[agent];
                      patch("routing", next);
                    }}
                  />
                </li>
              ))}
            </ul>
          </Field>
          <Field
            label="Резервные модели"
            htmlFor="ai-fallback"
            source={<Source web={form.fallback !== undefined} disabled={readOnly} onReset={() => patch("fallback", undefined)} />}
            hint="Через запятую, до пяти: если основная модель недоступна, агент пробует их по порядку."
          >
            <input id="ai-fallback" className="input mono" value={fallback} disabled={readOnly || provider === "none"} spellCheck={false} onChange={(e) => patch("fallback", e.target.value)} />
          </Field>
          <Field
            label="Бюджеты на один run"
            source={<Source web={form.budgets !== undefined} disabled={readOnly} onReset={() => patch("budgets", undefined)} />}
            hint="Run, исчерпавший бюджет, останавливается оркестратором (FR-15)."
          >
            <div className="ai-budgets">
              {BUDGETS.map((b) => (
                <label key={b.id}>
                  {b.label}
                  <input
                    className="input"
                    type="number"
                    min={b.id === "max_duration_minutes" ? 1 : 0}
                    step={b.step}
                    inputMode="decimal"
                    disabled={readOnly}
                    value={form.budgets?.[b.id] ?? String(file.budgets[b.id])}
                    onChange={(e) => patch("budgets", { ...(form.budgets ?? {}), [b.id]: e.target.value })}
                  />
                </label>
              ))}
            </div>
          </Field>
        </div>
      </Card>

      {!readOnly && (dirty || Object.keys(state.settings).length > 0 || serverError) && (
        <div className={dirty ? "ai-savebar dirty" : "ai-savebar"} role="region" aria-label="Сохранение настроек ИИ">
          <div className="grow stack">
            {issues.map((text) => (
              <span key={text} className="text-secondary detail" role="alert">
                {text}
              </span>
            ))}
            {serverError && (
              <span className="detail" role="alert">
                Сервер не принял настройки: {serverError}
              </span>
            )}
            {!issues.length && !serverError && <span className="text-secondary">{dirty ? "Есть несохранённые изменения" : "Часть настроек задана в веб"}</span>}
          </div>
          {Object.keys(state.settings).length > 0 && !dirty && (
            <Button variant="ghost" icon={<RotateCcw size={14} />} disabled={saving} onClick={resetAll}>
              Всё из .ai-coding.yml
            </Button>
          )}
          {dirty && (
            <Button variant="ghost" disabled={saving} onClick={() => (setForm(toForm(state.settings)), setServerError(null), setCustom(false))}>
              Отменить
            </Button>
          )}
          {dirty && (
            <Button variant="primary" icon={<Save size={14} />} loading={saving} disabled={issues.length > 0} onClick={() => void save(settings, "Настройки ИИ сохранены")}>
              Сохранить настройки
            </Button>
          )}
        </div>
      )}
    </>
  );
}

function Summary({ state }: { state: AIState }) {
  const e = state.effective;
  return (
    <Card span={2} title="Действует сейчас" icon={<Server size={15} />}>
      <dl className="kv">
        <dt>ИИ агентов</dt>
        <dd>{e.enabled ? <Badge tone="success" dot>включён</Badge> : <Badge tone="warning" dot>выключен</Badge>}</dd>
        <dt>Провайдер</dt>
        <dd>{NAMES[e.provider] ?? e.provider}</dd>
        <dt>Модель</dt>
        <dd className="mono">{e.model ?? "—"}</dd>
        {e.base_url && (
          <>
            <dt>Адрес</dt>
            <dd className="mono">{e.base_url}</dd>
          </>
        )}
        <dt>Ключ</dt>
        <dd>{e.provider === "ollama" ? <span className="text-muted">не нужен</span> : <KeyState state={state} />}</dd>
        <dt>Private Mode</dt>
        <dd>
          {e.private ? (
            <Badge tone="accent" dot>
              <Lock size={11} /> local-only
            </Badge>
          ) : (
            <Badge>{e.network}</Badge>
          )}
        </dd>
        <dt>Бюджет run</dt>
        <dd>
          {formatNumber(e.budgets.max_tokens_per_run)} ток. · {formatCost(e.budgets.max_cost_usd_per_run)} · {e.budgets.max_duration_minutes} мин
        </dd>
        {state.updated_at && (
          <>
            <dt>Изменено в веб</dt>
            <dd>
              {state.updated_by}, {relativeTime(state.updated_at)}
            </dd>
          </>
        )}
      </dl>
    </Card>
  );
}

function byAgent(executions: AgentExecution[]) {
  const map = new Map<string, { agent: string; runs: number; models: Set<string>; tokens: number; cost: number; failed: number }>();
  for (const e of executions) {
    const row = map.get(e.agent_name) ?? { agent: e.agent_name, runs: 0, models: new Set<string>(), tokens: 0, cost: 0, failed: 0 };
    row.runs += 1;
    e.models.forEach((m) => row.models.add(m));
    row.tokens += e.input_tokens + e.output_tokens;
    row.cost += e.cost_usd;
    if (e.status.toUpperCase() === "FAILED") row.failed += 1;
    map.set(e.agent_name, row);
  }
  return [...map.values()].sort((a, b) => b.cost - a.cost || b.runs - a.runs);
}

function Usage({ projectId }: { projectId: string }) {
  const [days, setDays] = useState<"7" | "30" | "90">("30");
  const usage = useUsage(projectId, Number(days));
  return (
    <Card
      title="Использование моделей"
      icon={<Coins size={15} />}
      actions={
        <Segmented
          label="Период"
          value={days}
          onChange={setDays}
          options={[
            { value: "7", label: "7 дн." },
            { value: "30", label: "30 дн." },
            { value: "90", label: "90 дн." },
          ]}
        />
      }
    >
      <QueryState query={usage} skeleton={<Skeleton height={180} />}>
        {(u) =>
          u.totals.calls === 0 ? (
            <EmptyState icon={<Coins size={20} />} title="Агенты оркестратора не обращались к моделям за этот период">
              Чат Claude Code и Codex работает по вашей подписке и здесь не учитывается.
            </EmptyState>
          ) : (
            <div className="stack">
              <div className="stats">
                <Stat label="Стоимость" value={formatCost(u.totals.cost_usd)} />
                <Stat label="Входные токены" value={formatNumber(u.totals.input_tokens)} />
                <Stat label="Выходные токены" value={formatNumber(u.totals.output_tokens)} />
                <Stat label="Вызовы" value={formatNumber(u.totals.calls)} />
              </div>
              <div className="chart" aria-label="Токены и стоимость по дням">
                <ResponsiveContainer width="100%" height={220}>
                  <ComposedChart data={u.daily.map((d) => ({ ...d, tokens: d.input_tokens + d.output_tokens }))} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid stroke="var(--border-primary)" vertical={false} />
                    <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 11 }} tickLine={false} axisLine={false} />
                    <YAxis yAxisId="tokens" tick={{ fill: "var(--text-muted)", fontSize: 11 }} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(Number(v))} width={48} />
                    <YAxis yAxisId="cost" orientation="right" tick={{ fill: "var(--text-muted)", fontSize: 11 }} tickLine={false} axisLine={false} tickFormatter={(v) => `$${Number(v).toFixed(2)}`} width={52} />
                    <ChartTooltip
                      contentStyle={{ background: "var(--bg-elevated)", border: "1px solid var(--border-secondary)", borderRadius: 8, fontSize: 12 }}
                      formatter={(v, name) => (name === "Стоимость" ? formatCost(Number(v)) : formatNumber(Number(v)))}
                    />
                    <Bar yAxisId="tokens" dataKey="tokens" name="Токены" fill="var(--accent-soft)" stroke="var(--accent-primary)" radius={[3, 3, 0, 0]} />
                    <Line yAxisId="cost" type="monotone" dataKey="cost_usd" name="Стоимость" stroke="var(--accent-cyan)" strokeWidth={2} dot={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
              <DataTable
                label="По моделям"
                rows={u.by_model}
                rowKey={(m) => `${m.provider}/${m.model}`}
                initialSort={{ id: "cost", desc: true }}
                columns={[
                  { id: "model", header: "Модель", cell: (m) => <span className="mono">{`${m.provider}/${m.model}`}</span>, sort: (m) => m.model },
                  { id: "calls", header: "Вызовы", cell: (m) => formatNumber(m.calls), sort: (m) => m.calls },
                  { id: "tokens", header: "Токены (вход / выход)", cell: (m) => `${formatNumber(m.input_tokens)} / ${formatNumber(m.output_tokens)}`, sort: (m) => m.input_tokens + m.output_tokens },
                  { id: "cost", header: "Стоимость", cell: (m) => formatCost(m.cost_usd), sort: (m) => m.cost_usd },
                ]}
              />
            </div>
          )
        }
      </QueryState>
    </Card>
  );
}

function Agents({ projectId }: { projectId: string }) {
  const executions = useAgentExecutions(projectId);
  const rows = useMemo(() => byAgent(executions.data ?? []), [executions.data]);
  return (
    <Card title="Модели по агентам" icon={<Cpu size={15} />}>
      <QueryState query={executions} skeleton={<Skeleton lines={3} />}>
        {() =>
          rows.length === 0 ? (
            <p className="text-muted tile-empty">Агенты оркестратора в этом проекте ещё не работали.</p>
          ) : (
            <DataTable
              label="Агенты и их модели"
              rows={rows}
              rowKey={(r) => r.agent}
              columns={[
                { id: "agent", header: "Агент", cell: (r) => <span className="cell-main">{r.agent}</span>, sort: (r) => r.agent },
                { id: "models", header: "Модели", cell: (r) => (r.models.size ? [...r.models].map((m) => <Badge key={m}>{m}</Badge>) : <span className="text-muted">без LLM</span>) },
                { id: "runs", header: "Выполнений", cell: (r) => `${r.runs}${r.failed ? ` · ошибок ${r.failed}` : ""}`, sort: (r) => r.runs },
                { id: "tokens", header: "Токены", cell: (r) => formatNumber(r.tokens), sort: (r) => r.tokens },
                { id: "cost", header: "Стоимость", cell: (r) => formatCost(r.cost), sort: (r) => r.cost },
              ]}
            />
          )
        }
      </QueryState>
    </Card>
  );
}


export default function Models() {
  const projectId = useUi((s) => s.projectId);
  const project = useProject(projectId);
  const ai = useAI(projectId);
  if (!projectId)
    return (
      <div className="page">
        <PageHeader icon={<BrainCircuit size={18} />} title="Модели и провайдеры" />
        <EmptyState title="Выберите проект" action={<LinkButton href={href("projects")}>Проекты</LinkButton>}>
          ИИ-провайдер оркестратора настраивается для каждого проекта.
        </EmptyState>
      </div>
    );
  return (
    <div className="page">
      <PageHeader
        icon={<BrainCircuit size={18} />}
        title="Модели и провайдеры"
        subtitle={`ИИ-провайдер агентов оркестратора в проекте «${project.data?.name ?? "…"}»: провайдер, модель, ключ, режим приватности, бюджеты и расход.`}
      />
      <div className="stack">
        <QueryState query={ai} skeleton={<Skeleton lines={6} />}>
          {(state) => <AIEditor key={projectId} projectId={projectId} state={state} />}
        </QueryState>
        <Usage projectId={projectId} />
        <Agents projectId={projectId} />
        <Card title="Чат и файл проекта" icon={<BrainCircuit size={15} />}>
          <p className="text-secondary note">
            Чат Claude Code и Codex в AI Workspace работает по вашей собственной подписке Claude или ChatGPT — эти настройки и расходы на этой странице его не касаются.
          </p>
          <p className="text-secondary note">
            Без веба провайдер задаётся в <code>.ai-coding.yml</code> проекта (ключ — именем переменной окружения сервера). Значения, заданные здесь, действуют поверх
            файла для запусков на сервере; локальный CLI по-прежнему читает только файл.
          </p>
          <CodeBlock code={YAML} label=".ai-coding.yml" />
        </Card>
      </div>
    </div>
  );
}
