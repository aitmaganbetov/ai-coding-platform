import { useCallback, useEffect, useState } from "react";
import { TERMINAL, api, download, type ArtifactInfo, type RunDetail } from "./api";

const POLL_MS = 2000;

export function RunPage({ id }: { id: string }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const [r, a] = await Promise.all([api.run(id), api.artifacts(id)]);
      setRun(r);
      setArtifacts(a);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!run || TERMINAL.has(run.state)) return undefined;
    const timer = window.setTimeout(() => void load(), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [run, load]);

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (!run) return error ? <p className="error">{error}</p> : <p className="muted">Загрузка…</p>;
  const pending = run.approvals.filter((a) => a.status === "pending");
  return (
    <section>
      <p>
        <a href={`#/projects/${run.project_id}`}>← к проекту</a>
      </p>
      <h2>
        Прогон {run.id.slice(-12)}{" "}
        <span className={`state state-${run.state.toLowerCase()}`}>{run.state}</span>
      </h2>
      <p className="muted">
        {run.kind ?? run.mode} · {run.goal}
      </p>
      {run.error && <p className="error">{run.error}</p>}
      {error && <p className="error">{error}</p>}

      {pending.length > 0 && (
        <div className="card">
          <h3>Ожидает подтверждения</h3>
          <ul>
            {pending.map((a) => (
              <li key={a.id}>
                <strong>{a.operation}</strong>: {a.reason}
              </li>
            ))}
          </ul>
          <input
            name="note"
            placeholder="комментарий"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button type="button" onClick={() => void act(() => api.decide(id, true, note))}>
            Подтвердить
          </button>
          <button type="button" onClick={() => void act(() => api.decide(id, false, note))}>
            Отклонить
          </button>
        </div>
      )}
      {!TERMINAL.has(run.state) && (
        <p>
          <button type="button" onClick={() => void act(() => api.cancel(id))}>
            Отменить прогон
          </button>
        </p>
      )}

      <h3>Задачи</h3>
      <table>
        <thead>
          <tr>
            <th>Шаг</th>
            <th>Фаза</th>
            <th>Статус</th>
            <th>Попытки</th>
            <th>Ошибка</th>
          </tr>
        </thead>
        <tbody>
          {run.tasks.map((t) => (
            <tr key={t.key}>
              <td>
                <code>{t.key}</code>
              </td>
              <td>{t.phase}</td>
              <td>{t.status}</td>
              <td>{t.attempts}</td>
              <td>{t.error ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Артефакты</h3>
      {artifacts.length === 0 ? (
        <p className="muted">Артефактов нет.</p>
      ) : (
        <ul>
          {artifacts.map((a) => (
            <li key={a.id}>
              <button
                type="button"
                className="link"
                onClick={() => void download(`/runs/${id}/artifacts/${a.id}`, a.name)}
              >
                {a.name}
              </button>{" "}
              <span className="muted">
                {a.media_type}, {a.size_bytes} Б
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
