import { BrainCircuit, Cloud, Coins, Cpu, Lock, Server, Wallet } from "lucide-react";
import { useMemo, useState } from "react";
import { Bar, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";
import type { AgentExecution } from "../api";
import { useAgentExecutions, useProject, useUsage } from "../data/queries";
import { href } from "../shell/routes";
import { useUi } from "../shell/store";
import {
  Badge,
  Card,
  CodeBlock,
  DataTable,
  EmptyState,
  formatCost,
  formatNumber,
  LinkButton,
  PageHeader,
  QueryState,
  Segmented,
  Skeleton,
  Stat,
} from "../ui";
import "./quality.css";

// AI models of the orchestrator's agents for the current project: provider, private mode,
// budgets and usage. The API never returns keys and this page never asks for one: the
// provider is configured in the project's .ai-coding.yml with the NAME of an environment
// variable that holds the key on the server.

const PROVIDERS = [
  { id: "anthropic", name: "Anthropic", kind: "cloud", text: "Claude по API-ключу Anthropic Console" },
  { id: "openai", name: "OpenAI", kind: "cloud", text: "GPT по API-ключу OpenAI" },
  { id: "ollama", name: "Ollama", kind: "local", text: "Локальные модели на вашем сервере" },
  { id: "vllm", name: "vLLM", kind: "local", text: "Свой OpenAI-совместимый сервер моделей" },
] as const;

const YAML = `ai:
  enabled: true
  provider: anthropic          # openai | anthropic | ollama | vllm
  model: <model-id>
  api_key_env: ANTHROPIC_API_KEY  # имя переменной окружения на сервере, не сам ключ

security:
  network: allowlist           # local-only запрещает внешние LLM (Private Mode)
  egress_allowlist: [api.anthropic.com]`;

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
  if (!projectId)
    return (
      <div className="page">
        <PageHeader icon={<BrainCircuit size={18} />} title="Модели и провайдеры" />
        <EmptyState title="Выберите проект" action={<LinkButton href={href("projects")}>Проекты</LinkButton>}>
          ИИ-провайдер оркестратора настраивается для каждого проекта.
        </EmptyState>
      </div>
    );
  const config = project.data?.config;
  const ai = config?.ai;
  const privateMode = config?.security.network === "local-only";
  return (
    <div className="page">
      <PageHeader
        icon={<BrainCircuit size={18} />}
        title="Модели и провайдеры"
        subtitle={`ИИ-провайдер агентов оркестратора в проекте «${project.data?.name ?? "…"}»: провайдер, модель, режим приватности, бюджеты и расход.`}
      />
      <QueryState query={project} skeleton={<Skeleton lines={4} />}>
        {() =>
          !config ? (
            <EmptyState title="Конфигурация проекта не прочитана">Сервер не смог прочитать .ai-coding.yml проекта — проверьте его командой «ai-coding config validate».</EmptyState>
          ) : (
            <div className="stack">
              <div className="bento">
                <Card span={2} title="Оркестратор проекта" icon={<Server size={15} />}>
                  <dl className="kv">
                    <dt>ИИ агентов</dt>
                    <dd>{ai?.enabled ? <Badge tone="success" dot>включён</Badge> : <Badge tone="warning" dot>выключен</Badge>}</dd>
                    <dt>Провайдер</dt>
                    <dd className="mono">{ai?.provider ?? "none"}</dd>
                    <dt>Модель</dt>
                    <dd className="mono">{ai?.model ?? "—"}</dd>
                    <dt>Private Mode</dt>
                    <dd>
                      {privateMode ? (
                        <Badge tone="accent" dot>
                          <Lock size={11} /> local-only
                        </Badge>
                      ) : (
                        <Badge>{config.security.network}</Badge>
                      )}
                    </dd>
                  </dl>
                  {privateMode && (
                    <p className="text-secondary note">
                      Private Mode: политика проекта <code>security.network: local-only</code> запрещает агентам оркестратора внешние LLM и любой выход в
                      сеть — доступны только локальные Ollama и vLLM.
                    </p>
                  )}
                </Card>
                <Card span={2} title="Бюджеты на один run" icon={<Wallet size={15} />}>
                  <div className="stats">
                    <Stat label="токенов" value={formatNumber(config.budgets.max_tokens_per_run)} />
                    <Stat label="стоимость" value={formatCost(config.budgets.max_cost_usd_per_run)} />
                    <Stat label="минут" value={config.budgets.max_duration_minutes} />
                  </div>
                  <p className="text-muted note">Run, исчерпавший бюджет, останавливается оркестратором (FR-15).</p>
                </Card>
              </div>
              <div className="provider-grid" role="list" aria-label="Провайдеры">
                {PROVIDERS.map((p) => {
                  const configured = ai?.provider === p.id;
                  const forbidden = privateMode && p.kind === "cloud";
                  const state = configured && ai?.enabled ? "настроен в .ai-coding.yml" : configured ? "указан, но ИИ выключен" : forbidden ? "запрещён политикой local-only" : "не используется";
                  return (
                    <div key={p.id} role="listitem" className={configured ? "provider-card current" : "provider-card"}>
                      <div className="row">
                        {p.kind === "cloud" ? <Cloud size={16} aria-hidden /> : <Server size={16} aria-hidden />}
                        <strong>{p.name}</strong>
                        <span className="card-spacer" />
                        <Badge tone={configured && ai?.enabled ? "success" : forbidden ? "danger" : "neutral"}>{state}</Badge>
                      </div>
                      <small className="text-muted">{p.text}</small>
                    </div>
                  );
                })}
              </div>
              <Usage projectId={projectId} />
              <Agents projectId={projectId} />
              <Card title="Как подключить или сменить провайдера" icon={<BrainCircuit size={15} />}>
                <p className="text-secondary note">
                  Провайдер задаётся в <code>.ai-coding.yml</code> проекта (схема проверяется сервером). Ключ хранится только в переменной окружения сервера —
                  в файле указывается её имя, платформа ключ не показывает и не принимает через веб.
                </p>
                <CodeBlock code={YAML} label=".ai-coding.yml" />
                <p className="text-muted note">
                  Чат Claude Code и Codex в AI Workspace работает по вашей подписке Claude или ChatGPT и в расходах этой страницы не учитывается.
                </p>
              </Card>
            </div>
          )
        }
      </QueryState>
    </div>
  );
}
