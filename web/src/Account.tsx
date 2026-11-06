import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ROLES,
  api,
  errorText,
  shortDate,
  type Me,
  type NewToken,
  type Project,
  type TokenRow,
} from "./api";
import { PasswordFields } from "./Users";

export function Account({ me, onLogout }: { me: Me; onLogout: () => void }) {
  return (
    <section>
      <h2>Профиль и токены</h2>
      <p>
        Вы вошли как <b>{me.username}</b>
        {me.is_admin ? " — администратор сервера" : ""}.
      </p>
      <ChangePassword />
      <Tokens me={me} onLogout={onLogout} />
    </section>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setDone(false);
    if (password !== repeat) {
      setError("Пароли не совпадают");
      return;
    }
    setBusy(true);
    try {
      await api.changePassword(current, password);
      setDone(true);
      setCurrent("");
      setPassword("");
      setRepeat("");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-label="Смена пароля">
      <h3>Смена пароля</h3>
      <label>
        Текущий пароль
        <input
          name="current-password"
          type="password"
          autoComplete="current-password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          required
        />
      </label>
      <PasswordFields
        label="Новый пароль"
        password={password}
        repeat={repeat}
        onPassword={setPassword}
        onRepeat={setRepeat}
      />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {done && (
        <p className="notice" role="status">
          Пароль изменён. Текущая сессия продолжает работать.
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          Сменить пароль
        </button>
      </div>
    </form>
  );
}

function isLive(t: TokenRow, now: number): boolean {
  return !t.revoked && (t.expires_at === null || Date.parse(t.expires_at) > now);
}

function Tokens({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [tokens, setTokens] = useState<TokenRow[] | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [showAll, setShowAll] = useState(false);
  const [issued, setIssued] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [t, p] = await Promise.all([api.tokens(), api.projects()]);
      setTokens(t);
      setProjects(p.filter((x) => x.role !== null));
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function revoke(id: string) {
    setError(null);
    try {
      await api.revokeToken(id);
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  const now = Date.now();
  const names = new Map(projects.map((p) => [p.id, p.name]));
  const shown = (tokens ?? []).filter((t) => showAll || isLive(t, now)).reverse();
  return (
    <>
      <h3>Токены API</h3>
      <p className="muted">
        Токены нужны CLI, CI (например, GitHub Actions) и скриптам: заголовок{" "}
        <code>Authorization: Bearer aic_…</code>. Токен с проектом действует только в нём, роль
        ограничивает права сверху.
      </p>
      <IssueToken
        projects={projects}
        onIssued={(token) => {
          setIssued(token);
          void load();
        }}
      />
      {issued && <IssuedToken token={issued} onClose={() => setIssued(null)} />}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <label className="check">
        <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
        Показывать отозванные и истёкшие
      </label>
      {tokens && (
        <table>
          <thead>
            <tr>
              <th>Название</th>
              <th>Проект</th>
              <th>Макс. роль</th>
              <th>Создан</th>
              <th>Истекает</th>
              <th>Использован</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => {
              const current = t.id === me.token_id;
              return (
                <tr key={t.id} className={isLive(t, now) ? undefined : "inactive"}>
                  <td>
                    {t.name}
                    {current && <span className="badge">текущая сессия</span>}
                  </td>
                  <td>{t.project_id ? (names.get(t.project_id) ?? t.project_id) : "все"}</td>
                  <td>{t.max_role ?? "—"}</td>
                  <td>{shortDate(t.created_at)}</td>
                  <td>{shortDate(t.expires_at)}</td>
                  <td>{shortDate(t.last_used_at)}</td>
                  <td>
                    {t.revoked ? (
                      <span className="muted">отозван</span>
                    ) : !isLive(t, now) ? (
                      <span className="muted">истёк</span>
                    ) : current ? (
                      <button type="button" onClick={onLogout}>
                        Выйти
                      </button>
                    ) : (
                      <button type="button" className="danger" onClick={() => void revoke(t.id)}>
                        Отозвать
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

function IssueToken({
  projects,
  onIssued,
}: {
  projects: Project[];
  onIssued: (token: string) => void;
}) {
  const [name, setName] = useState("");
  const [projectId, setProjectId] = useState("");
  const [role, setRole] = useState("");
  const [hours, setHours] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body: NewToken = {
      name: name.trim(),
      ...(projectId ? { project_id: projectId } : {}),
      ...(role ? { max_role: role } : {}),
      ...(hours ? { ttl_hours: Number(hours) } : {}),
    };
    try {
      const { token } = await api.issueToken(body);
      setName("");
      onIssued(token);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card inline" onSubmit={submit} aria-label="Новый токен">
      <label className="grow">
        Название
        <input
          name="token-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="например, github-actions"
          maxLength={100}
          required
        />
      </label>
      <label>
        Проект
        <select name="token-project" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">все мои проекты</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Макс. роль
        <select name="token-role" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="">как у меня</option>
          {ROLES.map((r) => (
            <option key={r}>{r}</option>
          ))}
        </select>
      </label>
      <label>
        Срок, часов
        <input
          name="token-ttl"
          type="number"
          min={1}
          max={24 * 365}
          value={hours}
          onChange={(e) => setHours(e.target.value)}
          placeholder="30 дней"
        />
      </label>
      <button type="submit" disabled={busy}>
        Выпустить токен
      </button>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

function IssuedToken({ token, onClose }: { token: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const canCopy = typeof navigator !== "undefined" && navigator.clipboard !== undefined;
  return (
    <div className="card notice-card" role="status">
      <p>
        <b>Скопируйте токен сейчас</b> — он показывается один раз, на сервере хранится только его
        хэш.
      </p>
      <div className="actions">
        <input
          className="secret grow"
          aria-label="Новый токен"
          readOnly
          value={token}
          onFocus={(e) => e.currentTarget.select()}
        />
        {canCopy && (
          <button
            type="button"
            onClick={() =>
              void navigator.clipboard.writeText(token).then(
                () => setCopied(true),
                () => setCopied(false),
              )
            }
          >
            {copied ? "Скопировано" : "Копировать"}
          </button>
        )}
        <button type="button" onClick={onClose}>
          Готово
        </button>
      </div>
    </div>
  );
}
