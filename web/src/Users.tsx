import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, errorText, shortDate, type Me, type UserRow } from "./api";

const MIN_PASSWORD = 12;

export function Users({ me }: { me: Me }) {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [resetFor, setResetFor] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    try {
      setUsers(await api.users());
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(fn: () => Promise<unknown>, done: string) {
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(done);
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <section>
      <div className="section-head">
        <h2>Пользователи</h2>
        {!adding && (
          <button type="button" className="primary" onClick={() => setAdding(true)}>
            Добавить пользователя
          </button>
        )}
      </div>
      {adding && (
        <NewUserForm
          onCancel={() => setAdding(false)}
          onCreated={(name) => {
            setAdding(false);
            setError(null);
            setNotice(`Пользователь ${name} создан. Добавьте его в проекты на их страницах.`);
            void load();
          }}
        />
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {resetFor && (
        <ResetPasswordForm
          username={resetFor}
          onCancel={() => setResetFor(null)}
          onDone={() => {
            setNotice(`Пароль пользователя ${resetFor} изменён.`);
            setResetFor(null);
          }}
        />
      )}
      {!users ? (
        !error && <p className="muted">Загрузка…</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Пользователь</th>
              <th>Имя</th>
              <th>Права</th>
              <th>Статус</th>
              <th>Создан</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const self = u.username === me.username;
              return (
                <tr key={u.id} className={u.is_active ? undefined : "inactive"}>
                  <td>
                    {u.username}
                    {self && <span className="muted"> (вы)</span>}
                  </td>
                  <td>{u.display_name ?? "—"}</td>
                  <td>{u.is_admin ? <span className="badge">администратор</span> : "—"}</td>
                  <td>
                    {u.is_active ? (
                      <span className="state state-completed">активен</span>
                    ) : (
                      <span className="state state-failed">заблокирован</span>
                    )}
                  </td>
                  <td>{shortDate(u.created_at)}</td>
                  <td>
                    {self ? (
                      <span className="muted">
                        Свой пароль — в разделе <a href="#/account">«Профиль и токены»</a>
                      </span>
                    ) : (
                      <div className="actions">
                        <button
                          type="button"
                          onClick={() =>
                            void act(
                              () => api.updateUser(u.username, { is_admin: !u.is_admin }),
                              u.is_admin
                                ? `${u.username} больше не администратор.`
                                : `${u.username} теперь администратор.`,
                            )
                          }
                        >
                          {u.is_admin ? "Снять права администратора" : "Сделать администратором"}
                        </button>
                        <button
                          type="button"
                          className={u.is_active ? "danger" : undefined}
                          onClick={() =>
                            void act(
                              () => api.updateUser(u.username, { is_active: !u.is_active }),
                              u.is_active
                                ? `${u.username} заблокирован, его токены отозваны.`
                                : `${u.username} разблокирован.`,
                            )
                          }
                        >
                          {u.is_active ? "Заблокировать" : "Разблокировать"}
                        </button>
                        <button type="button" onClick={() => setResetFor(u.username)}>
                          Сменить пароль
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <p className="muted">
        Проекты пользователи создают сами и становятся их владельцами; права в проекте
        назначает владелец на странице проекта (раздел «Участники»). Администратор к чужим
        проектам доступа не получает, пока его не добавят участником.
      </p>
    </section>
  );
}

function NewUserForm({
  onCreated,
  onCancel,
}: {
  onCreated: (username: string) => void;
  onCancel: () => void;
}) {
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password !== repeat) {
      setError("Пароли не совпадают");
      return;
    }
    setBusy(true);
    try {
      const name = username.trim();
      await api.createUser({
        username: name,
        password,
        is_admin: isAdmin,
        ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
      });
      onCreated(name);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-label="Новый пользователь">
      <h3>Новый пользователь</h3>
      <label>
        Логин
        <input
          name="username"
          autoComplete="off"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          pattern="\S+"
          title="Без пробелов"
          maxLength={150}
          required
        />
      </label>
      <label>
        Отображаемое имя
        <input
          name="display_name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          maxLength={200}
        />
      </label>
      <PasswordFields
        password={password}
        repeat={repeat}
        onPassword={setPassword}
        onRepeat={setRepeat}
      />
      <label className="check">
        <input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} />
        Администратор сервера (управляет пользователями)
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          Создать пользователя
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Отмена
        </button>
      </div>
    </form>
  );
}

function ResetPasswordForm({
  username,
  onDone,
  onCancel,
}: {
  username: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password !== repeat) {
      setError("Пароли не совпадают");
      return;
    }
    setBusy(true);
    try {
      await api.resetPassword(username, password);
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-label="Смена пароля пользователя">
      <h3>Новый пароль для {username}</h3>
      <PasswordFields
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
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          Сохранить пароль
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Отмена
        </button>
      </div>
    </form>
  );
}

export function PasswordFields({
  password,
  repeat,
  onPassword,
  onRepeat,
  label = "Пароль",
}: {
  password: string;
  repeat: string;
  onPassword: (value: string) => void;
  onRepeat: (value: string) => void;
  label?: string;
}) {
  return (
    <>
      <label>
        {label}
        <input
          name="new-password"
          type="password"
          autoComplete="new-password"
          minLength={MIN_PASSWORD}
          value={password}
          onChange={(e) => onPassword(e.target.value)}
          required
        />
        <span className="muted hint">Не короче {MIN_PASSWORD} символов.</span>
      </label>
      <label>
        Повторите пароль
        <input
          name="repeat-password"
          type="password"
          autoComplete="new-password"
          value={repeat}
          onChange={(e) => onRepeat(e.target.value)}
          required
        />
      </label>
    </>
  );
}
