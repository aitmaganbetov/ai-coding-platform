import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, errorText, type NewProject, type ProjectSources, type SourceDirectory } from "./api";
import { buildTar, selectFolder, type FolderSelection } from "./tar";

type Source = "upload" | "git" | "path";

// The server accepts letters, digits, '.', '_' and '-' (sanitize_project_name).
export function projectNameFrom(source: string): string {
  const last = source.trim().replace(/[/\\]+$/, "").split(/[/\\:]/).pop() ?? "";
  return last
    .replace(/\.git$/i, "")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .slice(0, 100);
}

export function formatBytes(bytes: number): string {
  const round = (value: number) => String(Number(value.toFixed(1)));
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 ** 2) return `${round(bytes / 1024)} КБ`;
  if (bytes < 1024 ** 3) return `${round(bytes / 1024 ** 2)} МБ`;
  return `${round(bytes / 1024 ** 3)} ГБ`;
}

function directoryLabel(d: SourceDirectory, name: string): string {
  const used = d.used ? (d.project ? ` — подключён: ${d.project}` : " — уже используется") : "";
  return `${name}${d.git ? " (git)" : ""}${used}`;
}

/** Connect a project. ``onCreated``: what happens next (onboarding); by default its page opens. */
export function NewProjectForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated?: (project: { id: string; name: string }) => void;
}) {
  const [sources, setSources] = useState<ProjectSources | null>(null);
  const [source, setSource] = useState<Source>("upload");
  const [name, setName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  const [folder, setFolder] = useState<FolderSelection | null>(null);
  const [gitUrl, setGitUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [gitToken, setGitToken] = useState("");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.projectSources().then(setSources, (err: unknown) => setError(errorText(err)));
  }, []);

  // React does not know the folder-picker attributes: set them on the element.
  useEffect(() => {
    folderInput.current?.setAttribute("webkitdirectory", "");
    folderInput.current?.setAttribute("directory", "");
  }, [source]);

  const roots = sources?.roots ?? [];
  const canUsePath = roots.length > 0;
  const limit = sources?.upload_max_bytes ?? 0;
  const tooBig = folder !== null && limit > 0 && folder.bytes > limit;

  function follow(value: string) {
    if (!nameEdited) setName(projectNameFrom(value));
  }

  function pickFolder(files: FileList | null) {
    setError(null);
    if (!files || files.length === 0) {
      setFolder(null);
      return;
    }
    const picked = selectFolder(files);
    setFolder(picked);
    follow(picked.folder);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (source === "upload" && (!folder || folder.entries.length === 0)) {
      setError("Выберите папку с файлами проекта");
      return;
    }
    setBusy(true);
    try {
      let project;
      if (source === "upload" && folder) {
        project = await api.uploadProject(name.trim(), buildTar(folder.entries));
      } else {
        const body: NewProject =
          source === "git"
            ? {
                name: name.trim(),
                git_url: gitUrl.trim(),
                ...(branch.trim() ? { branch: branch.trim() } : {}),
                ...(gitToken.trim() ? { git_token: gitToken.trim() } : {}),
              }
            : { name: name.trim(), path: path.trim() };
        project = await api.createProject(body);
      }
      setGitToken("");
      if (onCreated) onCreated({ id: project.id, name: project.name });
      else window.location.hash = `#/projects/${project.id}`;
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  const busyLabel =
    source === "upload"
      ? `Загрузка ${folder ? formatBytes(folder.bytes) : ""}…`
      : source === "git"
        ? "Клонирование…"
        : "Подключение…";

  return (
    <form className="card form" onSubmit={submit} aria-label="Новый проект">
      <h3>Новый проект</h3>
      <fieldset className="choice">
        <legend>Откуда взять код</legend>
        <label className="check">
          <input
            type="radio"
            name="source"
            value="upload"
            checked={source === "upload"}
            onChange={() => setSource("upload")}
          />
          Папка с моего компьютера
        </label>
        <label className="check">
          <input
            type="radio"
            name="source"
            value="git"
            checked={source === "git"}
            onChange={() => setSource("git")}
          />
          Git-репозиторий (GitHub, GitLab и др.)
        </label>
        <label className="check">
          <input
            type="radio"
            name="source"
            value="path"
            checked={source === "path"}
            disabled={!canUsePath}
            onChange={() => setSource("path")}
          />
          Папка на сервере
        </label>
        {sources && !canUsePath && (
          <p className="muted hint">
            Папки на сервере недоступны: администратор не задал{" "}
            <code>AI_CODING_SERVER_ALLOWED_ROOTS</code>.
          </p>
        )}
      </fieldset>

      {source === "upload" && (
        <label>
          Папка проекта
          <input
            ref={folderInput}
            name="folder"
            type="file"
            multiple
            onChange={(e) => pickFolder(e.target.files)}
          />
          <span className="muted hint">
            Загружаются файлы проекта без node_modules, виртуальных окружений, кэшей и папки
            .git: на сервере создаётся новый репозиторий с одним коммитом. Чтобы сохранить историю
            коммитов, подключите проект как Git-репозиторий.
            {limit > 0 ? ` Не больше ${formatBytes(limit)}.` : ""}
          </span>
        </label>
      )}
      {source === "upload" && folder && (
        <p className={tooBig ? "error" : "muted"} role={tooBig ? "alert" : undefined}>
          {folder.folder ? `«${folder.folder}»: ` : ""}
          {folder.entries.length} файл(ов), {formatBytes(folder.bytes)}
          {tooBig ? ` — больше допустимых ${formatBytes(limit)}` : ""}
          {folder.skipped.size > 0 &&
            `. Пропущено: ${Array.from(folder.skipped, ([dir, n]) => `${dir} (${n})`).join(", ")}`}
        </p>
      )}

      {source === "git" && (
        <>
          <label>
            URL репозитория
            <input
              name="git_url"
              value={gitUrl}
              onChange={(e) => {
                setGitUrl(e.target.value);
                follow(e.target.value);
              }}
              placeholder="https://github.com/org/repo.git"
              required
            />
          </label>
          <label>
            Ветка
            <input
              name="branch"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              placeholder="по умолчанию — основная ветка репозитория"
            />
          </label>
          <label>
            Токен доступа (только для приватного репозитория)
            <input
              name="git_token"
              type="password"
              autoComplete="new-password"
              value={gitToken}
              onChange={(e) => setGitToken(e.target.value)}
            />
            <span className="muted hint">
              GitHub: Settings → Developer settings → Fine-grained tokens, доступ «Contents:
              Read-only» к этому репозиторию. Работает с https://-адресом. Токен используется
              только для клонирования и нигде не сохраняется.
            </span>
          </label>
        </>
      )}

      {source === "path" && (
        <>
          <label>
            Каталог
            <select
              name="directory"
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                follow(e.target.value);
              }}
            >
              <option value="">— выберите каталог —</option>
              {roots.map((root) => (
                <optgroup key={root.path} label={root.path}>
                  {root.git && (
                    <option value={root.path} disabled={root.used}>
                      {directoryLabel(root, root.path)}
                    </option>
                  )}
                  {root.directories.map((d) => (
                    <option key={d.path} value={d.path} disabled={d.used}>
                      {directoryLabel(d, d.name)}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>
          <label>
            Путь
            <input
              name="path"
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                follow(e.target.value);
              }}
              placeholder={roots[0] ? `${roots[0].path}/my-project` : ""}
              required
            />
            <span className="muted hint">
              Любой свободный каталог внутри: {roots.map((r) => r.path).join(", ")}. Сервер
              работает с ним на месте и не удаляет его вместе с проектом.
            </span>
          </label>
        </>
      )}

      <label>
        Имя проекта
        <input
          name="name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setNameEdited(true);
          }}
          pattern="[A-Za-z0-9._\-]+"
          title="Латинские буквы, цифры, «.», «_» и «-»"
          maxLength={100}
          required
        />
        <span className="muted hint">
          Латинские буквы, цифры, «.», «_» и «-». Вы станете владельцем (owner) проекта и сможете
          добавить в него других пользователей.
        </span>
      </label>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || tooBig}>
          {busy ? busyLabel : "Создать проект"}
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Отмена
        </button>
      </div>
    </form>
  );
}
