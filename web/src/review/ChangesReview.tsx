import { useQuery } from "@tanstack/react-query";
import {
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  FileMinus2,
  FilePen,
  FilePlus2,
  FileSymlink,
  GitCommitHorizontal,
  RefreshCw,
  Undo2,
  X,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { api, type DiffFile, type DiffHunk, errorText, type ReviewAction, type WorkspaceDiff } from "../api";
import { queryClient } from "../data/queries";
import { Badge, Button, EmptyState, ErrorState, IconButton, Segmented, Skeleton, toast, type Tone } from "../ui";
import "./review.css";

// AI Changes Review (ADR-0040): the workspace's changes by file and hunk. Pending changes
// (what the agent or the user changed) are accepted one hunk at a time — staged — or
// rejected — reverted in the files; accepted ones can go back to review; "Commit
// accepted" commits the index alone. Git runs in the user's container (ADR-0036).

type Mode = "unified" | "split";
type Cell = { no: number | null; text: string; kind: "context" | "add" | "del" | "meta" | "empty" };

const STATUS: Record<DiffFile["status"], { label: string; tone: Tone; icon: typeof FilePen }> = {
  modified: { label: "изменён", tone: "info", icon: FilePen },
  added: { label: "новый", tone: "success", icon: FilePlus2 },
  deleted: { label: "удалён", tone: "danger", icon: FileMinus2 },
  renamed: { label: "переименован", tone: "accent", icon: FileSymlink },
};
const MODE_KEY = "nexus-review-mode";
const LINE_LIMIT = 1500; // lines rendered per file; the rest on demand

export const diffKey = (projectId: string) => ["diff", projectId] as const;

/** All hunks of a file as one patch (what "accept/reject the whole file" sends). */
export function filePatch(file: DiffFile): string {
  return file.header + file.hunks.map((h) => h.patch.slice(file.header.length)).join("");
}

/** Side-by-side rows of a hunk: deletions and additions paired, context on both sides. */
export function splitRows(hunk: DiffHunk): [Cell, Cell][] {
  const rows: [Cell, Cell][] = [];
  const empty: Cell = { no: null, text: "", kind: "empty" };
  let oldNo = hunk.old_start;
  let newNo = hunk.new_start;
  let dels: Cell[] = [];
  let adds: Cell[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(dels.length, adds.length); i++) rows.push([dels[i] ?? empty, adds[i] ?? empty]);
    dels = [];
    adds = [];
  };
  for (const line of hunk.lines) {
    if (line.kind === "del") dels.push({ no: oldNo++, text: line.text, kind: "del" });
    else if (line.kind === "add") adds.push({ no: newNo++, text: line.text, kind: "add" });
    else if (line.kind === "meta") {
      flush();
      rows.push([{ no: null, text: line.text, kind: "meta" }, { no: null, text: line.text, kind: "meta" }]);
    } else {
      flush();
      rows.push([{ no: oldNo++, text: line.text, kind: "context" }, { no: newNo++, text: line.text, kind: "context" }]);
    }
  }
  flush();
  return rows;
}

/** Unified rows with old and new line numbers. */
export function unifiedRows(hunk: DiffHunk): { old: number | null; new: number | null; kind: Cell["kind"]; text: string }[] {
  let oldNo = hunk.old_start;
  let newNo = hunk.new_start;
  return hunk.lines.map((line) => {
    if (line.kind === "del") return { old: oldNo++, new: null, kind: "del", text: line.text };
    if (line.kind === "add") return { old: null, new: newNo++, kind: "add", text: line.text };
    if (line.kind === "meta") return { old: null, new: null, kind: "meta", text: line.text };
    return { old: oldNo++, new: newNo++, kind: "context", text: line.text };
  });
}

function useNarrow(): boolean {
  const query = "(max-width: 720px)";
  const [narrow, setNarrow] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const media = window.matchMedia?.(query);
    const on = () => setNarrow(media?.matches ?? false);
    media?.addEventListener?.("change", on);
    return () => media?.removeEventListener?.("change", on);
  }, []);
  return narrow;
}

function readMode(): Mode {
  try {
    return window.localStorage.getItem(MODE_KEY) === "split" ? "split" : "unified";
  } catch {
    return "unified";
  }
}

const SIGN: Record<Cell["kind"], string> = { add: "+", del: "−", context: " ", meta: "", empty: "" };

function HunkView({ hunk, mode }: { hunk: DiffHunk; mode: Mode }) {
  if (mode === "split")
    return (
      <table className="diff-table split" role="table" aria-label={`Фрагмент ${hunk.header}`}>
        <tbody>
          {splitRows(hunk).map(([left, right], i) => (
            <tr key={i}>
              <td className={`ln ${left.kind}`}>{left.no ?? ""}</td>
              <td className={`code ${left.kind}`}>{left.kind === "empty" ? "" : left.text}</td>
              <td className={`ln ${right.kind}`}>{right.no ?? ""}</td>
              <td className={`code ${right.kind}`}>{right.kind === "empty" ? "" : right.text}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  return (
    <table className="diff-table unified" role="table" aria-label={`Фрагмент ${hunk.header}`}>
      <tbody>
        {unifiedRows(hunk).map((row, i) => (
          <tr key={i} className={row.kind}>
            <td className="ln">{row.old ?? ""}</td>
            <td className="ln">{row.new ?? ""}</td>
            <td className="sign" aria-hidden>
              {SIGN[row.kind]}
            </td>
            <td className="code">{row.text}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FileCard({
  file,
  layer,
  mode,
  busy,
  onAct,
  onOpenFile,
}: {
  file: DiffFile;
  layer: "pending" | "new" | "accepted";
  mode: Mode;
  busy: string | null;
  onAct: (key: string, body: { action: ReviewAction; patch?: string; path?: string }) => void;
  onOpenFile?: (path: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const [all, setAll] = useState(false);
  const info = STATUS[file.status] ?? STATUS.modified;
  const Icon = info.icon;
  const lines = file.hunks.reduce((n, h) => n + h.lines.length, 0);
  let budget = all ? Infinity : LINE_LIMIT;
  const shown = file.hunks.filter((h) => {
    const fits = budget > 0;
    budget -= h.lines.length;
    return fits;
  });
  const key = `${layer}:${file.path}`;
  const disabled = busy !== null;
  const fileActions =
    layer === "new" ? (
      <>
        <Button size="sm" variant="primary" icon={<Check size={13} />} disabled={disabled} onClick={() => onAct(`${key}:all`, { action: "accept_file", path: file.path })}>
          Принять файл
        </Button>
        <Button
          size="sm"
          variant="danger"
          icon={<X size={13} />}
          disabled={disabled}
          onClick={() => {
            if (window.confirm(`Удалить новый файл ${file.path}? Его содержимое не сохранится.`)) onAct(`${key}:all`, { action: "reject_file", path: file.path });
          }}
        >
          Удалить
        </Button>
      </>
    ) : file.binary || file.hunks.length === 0 ? (
      // No hunks to apply (binary, mode only): the whole file.
      layer === "pending" ? (
        <>
          <Button size="sm" icon={<CheckCheck size={13} />} disabled={disabled} onClick={() => onAct(`${key}:all`, { action: "accept_path", path: file.path })}>
            Принять файл
          </Button>
          <Button size="sm" variant="ghost" icon={<Undo2 size={13} />} disabled={disabled} onClick={() => onAct(`${key}:all`, { action: "reject_path", path: file.path })}>
            Отклонить файл
          </Button>
        </>
      ) : (
        <Button size="sm" variant="ghost" icon={<Undo2 size={13} />} disabled={disabled} onClick={() => onAct(`${key}:all`, { action: "unstage_path", path: file.path })}>
          Вернуть на проверку
        </Button>
      )
    ) : layer === "pending" ? (
      <>
        <Button size="sm" icon={<CheckCheck size={13} />} disabled={disabled} onClick={() => onAct(`${key}:all`, { action: "accept", patch: filePatch(file) })}>
          Принять файл
        </Button>
        <Button size="sm" variant="ghost" icon={<Undo2 size={13} />} disabled={disabled} onClick={() => onAct(`${key}:all`, { action: "reject", patch: filePatch(file) })}>
          Отклонить файл
        </Button>
      </>
    ) : (
      <Button size="sm" variant="ghost" icon={<Undo2 size={13} />} disabled={disabled} onClick={() => onAct(`${key}:all`, { action: "unstage", patch: filePatch(file) })}>
        Вернуть на проверку
      </Button>
    );
  return (
    <article className={`rfile layer-${layer}`} aria-label={`${file.path}: ${info.label}`}>
      <header className="rfile-head">
        <button type="button" className="rfile-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)} aria-label={open ? "Свернуть файл" : "Развернуть файл"}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        <Icon size={14} aria-hidden className={`tone-${info.tone}`} />
        <span className="rfile-path">
          {file.old_path && <span className="text-muted">{file.old_path} → </span>}
          {onOpenFile && file.status !== "deleted" ? (
            <button type="button" className="rfile-open" onClick={() => onOpenFile(file.path)} title="Открыть в редакторе">
              {file.path}
            </button>
          ) : (
            file.path
          )}
        </span>
        <Badge tone={info.tone}>{info.label}</Badge>
        <span className="rfile-stat">
          <span className="tone-success">+{file.additions}</span> <span className="tone-danger">−{file.deletions}</span>
        </span>
        <span className="card-spacer" />
        <span className="rfile-actions">{fileActions}</span>
      </header>
      {open && (
        <div className="rfile-body">
          {file.binary && <p className="text-muted rfile-note">Двоичный файл: построчно не показывается — принимается или откатывается целиком.</p>}
          {!file.binary && file.hunks.length === 0 && <p className="text-muted rfile-note">Изменились только свойства файла (режим или имя).</p>}
          {shown.map((hunk, i) => (
            <section key={`${hunk.header}-${i}`} className="hunk" aria-label={`Фрагмент ${i + 1} из ${file.hunks.length}`}>
              <div className="hunk-head">
                <code className="hunk-range">{hunk.header.split("@@")[1]?.trim() ? `@@ ${hunk.header.split("@@")[1]?.trim()} @@` : hunk.header}</code>
                {hunk.section && <span className="hunk-section truncate">{hunk.section}</span>}
                <span className="card-spacer" />
                {layer === "pending" && (
                  <>
                    <IconButton label={`Принять фрагмент ${i + 1}`} icon={<Check size={14} />} disabled={disabled} onClick={() => onAct(`${key}:${i}`, { action: "accept", patch: hunk.patch })} />
                    <IconButton label={`Отклонить фрагмент ${i + 1}`} icon={<X size={14} />} disabled={disabled} onClick={() => onAct(`${key}:${i}`, { action: "reject", patch: hunk.patch })} />
                  </>
                )}
                {layer === "accepted" && (
                  <IconButton label={`Вернуть фрагмент ${i + 1} на проверку`} icon={<Undo2 size={14} />} disabled={disabled} onClick={() => onAct(`${key}:${i}`, { action: "unstage", patch: hunk.patch })} />
                )}
              </div>
              <div className="hunk-body">
                <HunkView hunk={hunk} mode={mode} />
              </div>
            </section>
          ))}
          {shown.length < file.hunks.length && (
            <Button size="sm" variant="ghost" onClick={() => setAll(true)}>
              Показать остальное ({lines} строк всего)
            </Button>
          )}
        </div>
      )}
    </article>
  );
}

export function ChangesReview({
  projectId,
  onChanged,
  onOpenFile,
}: {
  projectId: string;
  /** Files changed (a hunk rejected, a new file removed): reload the tree and editor. */
  onChanged?: () => void;
  onOpenFile?: (path: string) => void;
}) {
  const diff = useQuery({ queryKey: diffKey(projectId), queryFn: () => api.workspaceDiff(projectId) });
  const [chosen, setChosen] = useState<Mode>(readMode);
  const narrow = useNarrow();
  const mode: Mode = narrow ? "unified" : chosen;
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  function chooseMode(next: Mode) {
    setChosen(next);
    try {
      window.localStorage.setItem(MODE_KEY, next);
    } catch {
      /* the choice lasts for this page only */
    }
  }

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: diffKey(projectId) });
  }

  async function act(key: string, body: { action: ReviewAction; patch?: string; path?: string }) {
    setBusy(key);
    try {
      await api.review(projectId, body);
      if (body.action.startsWith("reject")) onChanged?.();
    } catch (err) {
      toast({ tone: "danger", title: "Изменение не применено", text: `${errorText(err)} Список обновлён — проверьте ещё раз.` });
    } finally {
      await refresh();
      setBusy(null);
    }
  }

  async function acceptAll(data: WorkspaceDiff) {
    setBusy("all");
    try {
      for (const file of data.pending)
        await api.review(projectId, file.binary || !file.hunks.length ? { action: "accept_path", path: file.path } : { action: "accept", patch: filePatch(file) });
      for (const file of data.new_files) await api.review(projectId, { action: "accept_file", path: file.path });
      toast({ tone: "success", title: "Все изменения приняты" });
    } catch (err) {
      toast({ tone: "danger", title: "Приняты не все изменения", text: errorText(err) });
    } finally {
      await refresh();
      setBusy(null);
    }
  }

  async function commit(event: FormEvent) {
    event.preventDefault();
    setBusy("commit");
    try {
      const result = await api.commit(projectId, message, true);
      if (result.ok) {
        setMessage("");
        toast({ tone: "success", title: "Принятые изменения зафиксированы", text: result.branch ? `Ветка ${result.branch}` : undefined });
      } else {
        toast({ tone: "warning", title: "Нечего фиксировать", text: result.output });
      }
    } catch (err) {
      toast({ tone: "danger", title: "Коммит не выполнен", text: errorText(err) });
    } finally {
      await refresh();
      setBusy(null);
    }
  }

  const counts = useMemo(() => {
    const d = diff.data;
    if (!d) return null;
    const hunks = (files: DiffFile[]) => files.reduce((n, f) => n + Math.max(1, f.hunks.length), 0);
    return { pending: hunks(d.pending) + d.new_files.length, accepted: hunks(d.accepted), files: d.pending.length + d.new_files.length };
  }, [diff.data]);

  if (diff.isLoading) return <Skeleton lines={6} />;
  if (diff.error) return <ErrorState error={diff.error} onRetry={() => void diff.refetch()} />;
  const data = diff.data;
  if (!data || !counts) return null;
  const nothing = counts.pending === 0 && counts.accepted === 0;
  return (
    <div className="review" aria-busy={busy !== null}>
      <div className="review-bar">
        <div className="review-title">
          <strong>Проверка изменений</strong>
          <span className="text-muted">
            На проверке: {counts.pending} · принято: {counts.accepted}
          </span>
        </div>
        <span className="card-spacer" />
        <Segmented
          label="Вид изменений"
          value={mode}
          onChange={chooseMode}
          options={[
            { value: "unified", label: "Построчно" },
            { value: "split", label: "Рядом" },
          ]}
        />
        <IconButton label="Обновить" icon={<RefreshCw size={14} />} onClick={() => void diff.refetch()} disabled={busy !== null} />
        {counts.pending > 0 && (
          <Button size="sm" variant="primary" icon={<CheckCheck size={14} />} loading={busy === "all"} disabled={busy !== null && busy !== "all"} onClick={() => void acceptAll(data)}>
            Принять всё
          </Button>
        )}
      </div>
      {data.truncated && <p className="review-warn">Изменений слишком много: показана часть. Зафиксируйте или отклоните часть изменений и обновите список.</p>}
      {nothing ? (
        <EmptyState icon={<CheckCheck size={20} />} title="Изменений нет">
          Когда агент или вы измените файлы рабочей области, изменения появятся здесь: каждое можно принять или отклонить.
        </EmptyState>
      ) : (
        <>
          {counts.pending > 0 && (
            <section className="review-section" aria-label="На проверке">
              <h3 className="review-section-title">На проверке</h3>
              {data.pending.map((f) => (
                <FileCard key={`p-${f.path}`} file={f} layer="pending" mode={mode} busy={busy} onAct={(k, b) => void act(k, b)} onOpenFile={onOpenFile} />
              ))}
              {data.new_files.map((f) => (
                <FileCard key={`n-${f.path}`} file={f} layer="new" mode={mode} busy={busy} onAct={(k, b) => void act(k, b)} onOpenFile={onOpenFile} />
              ))}
            </section>
          )}
          {counts.accepted > 0 && (
            <section className="review-section" aria-label="Принято">
              <h3 className="review-section-title">Принято</h3>
              <form className="review-commit" onSubmit={(e) => void commit(e)}>
                <input className="input" aria-label="Сообщение коммита принятых изменений" placeholder="Сообщение коммита" value={message} onChange={(e) => setMessage(e.target.value)} required />
                <Button type="submit" size="sm" variant="primary" icon={<GitCommitHorizontal size={14} />} loading={busy === "commit"} disabled={busy !== null && busy !== "commit"}>
                  Зафиксировать принятое
                </Button>
              </form>
              {data.accepted.map((f) => (
                <FileCard key={`a-${f.path}`} file={f} layer="accepted" mode={mode} busy={busy} onAct={(k, b) => void act(k, b)} onOpenFile={onOpenFile} />
              ))}
            </section>
          )}
        </>
      )}
    </div>
  );
}

/** Pending hunks and new files (for a badge on the Diff tab). */
export function usePendingCount(projectId: string, enabled: boolean): number | undefined {
  const diff = useQuery({ queryKey: diffKey(projectId), queryFn: () => api.workspaceDiff(projectId), enabled, retry: false });
  if (!diff.data) return undefined;
  return diff.data.pending.reduce((n, f) => n + Math.max(1, f.hunks.length), 0) + diff.data.new_files.length;
}
