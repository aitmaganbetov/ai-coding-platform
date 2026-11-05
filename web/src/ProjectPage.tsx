import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  api,
  download,
  type AuditRow,
  type FindingRow,
  type ProjectDetail,
  type RunSummary,
} from "./api";

const CAN_RUN = new Set(["owner", "maintainer", "developer"]);
const CAN_AUDIT = new Set(["owner", "maintainer", "auditor"]);
const ROLES = ["owner", "maintainer", "developer", "auditor", "agent"];

export function ProjectPage({ id }: { id: string }) {
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [findings, setFindings] = useState<FindingRow[]>([]);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await api.project(id);
      setProject(detail);
      const [r, f] = await Promise.all([api.runs(id), api.findings(id)]);
      setRuns(r);
      setFindings(f);
      setAudit(CAN_AUDIT.has(detail.role ?? "") ? await api.audit(id) : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) return <p className="error">{error}</p>;
  if (!project) return <p className="muted">Загрузка…</p>;
  const role = project.role ?? "";
  return (
    <section>
      <h2>
        {project.name} <span className="badge">{role}</span>
      </h2>
      {CAN_RUN.has(role) && <StartRun project={project} onStarted={load} />}
      <h3>Прогоны</h3>
      <RunTable runs={runs} />
      <h3>Находки ({findings.length})</h3>
      <FindingTable findings={findings} />
      <h3>Участники</h3>
      <Members project={project} onChange={load} />
      {audit && (
        <>
          <h3>Журнал аудита</h3>
          <AuditTable rows={audit} />
        </>
      )}
      {role === "owner" && (
        <p>
          <button
            type="button"
            onClick={() => void download(`/projects/${id}/export`, `${project.name}-export.zip`)}
          >
            Экспорт данных проекта
          </button>
        </p>
      )}
    </section>
  );
}

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
    <form className="card inline" onSubmit={submit}>
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
        <input name="params" value={params} onChange={(e) => setParams(e.target.value)} />
      </label>
      <button type="submit">Запустить</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

export function RunTable({ runs }: { runs: RunSummary[] }) {
  if (runs.length === 0) return <p className="muted">Прогонов нет.</p>;
  return (
    <table>
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
              <a href={`#/runs/${r.id}`}>{r.id.slice(-12)}</a>
            </td>
            <td>{r.kind ?? r.mode}</td>
            <td>
              <span className={`state state-${r.state.toLowerCase()}`}>{r.state}</span>
            </td>
            <td>{r.requested_by}</td>
            <td>{r.created_at.slice(0, 19).replace("T", " ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FindingTable({ findings }: { findings: FindingRow[] }) {
  if (findings.length === 0) return <p className="muted">Находок нет.</p>;
  return (
    <table>
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
              <span className={`sev sev-${f.severity}`}>{f.severity}</span>
            </td>
            <td>{f.source}</td>
            <td>
              <code>{f.rule_id ?? "—"}</code>
            </td>
            <td>
              <code>
                {f.file_path ?? "—"}
                {f.line ? `:${f.line}` : ""}
              </code>
            </td>
            <td>{f.title}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Members({ project, onChange }: { project: ProjectDetail; onChange: () => void }) {
  const [username, setUsername] = useState("");
  const [role, setRole] = useState("developer");
  const [error, setError] = useState<string | null>(null);
  const owner = project.role === "owner";

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>
      <table>
        <tbody>
          {project.members.map((m) => (
            <tr key={m.username}>
              <td>{m.username}</td>
              <td>{m.role}</td>
              <td>
                {owner && (
                  <button
                    type="button"
                    onClick={() => void act(() => api.removeMember(project.id, m.username))}
                  >
                    Удалить
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {owner && (
        <form
          className="card inline"
          onSubmit={(e) => {
            e.preventDefault();
            void act(() => api.setMember(project.id, username, role));
          }}
        >
          <input
            name="member"
            placeholder="пользователь"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
          <select name="role" value={role} onChange={(e) => setRole(e.target.value)}>
            {ROLES.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
          <button type="submit">Назначить роль</button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
    </>
  );
}

function AuditTable({ rows }: { rows: AuditRow[] }) {
  return (
    <table>
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
            <td>{e.created_at.slice(0, 19).replace("T", " ")}</td>
            <td>{e.actor}</td>
            <td>
              <code>{e.action}</code>
            </td>
            <td>
              <span className={`outcome outcome-${e.outcome}`}>{e.outcome}</span>
            </td>
            <td>{e.target ?? (e.run_id ? e.run_id.slice(-12) : "")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
