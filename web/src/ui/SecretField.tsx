import { useQuery } from "@tanstack/react-query";
import { KeyRound, Trash2 } from "lucide-react";
import { type FormEvent, useState } from "react";
import { api, errorText } from "../api";
import { queryClient } from "../data/queries";
import { Badge, Button, relativeTime, toast } from "./index";

export const secretsKey = (projectId: string) => ["secrets", projectId] as const;

export function useSecrets(projectId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: secretsKey(projectId ?? ""),
    queryFn: () => api.secrets(projectId as string),
    enabled: Boolean(projectId) && enabled,
    retry: false,
  });
}

/**
 * A write-only secret of a project (an API key, a token): never shown once saved — only
 * its last characters and when it changed; it can be replaced or removed. The server
 * keeps it encrypted (ADR-0043, ADR-0046, ADR-0048).
 */
export function SecretField({
  projectId,
  name,
  label,
  placeholder,
  help,
  disabled,
}: {
  projectId: string;
  name: string;
  label: string;
  placeholder?: string;
  help?: string;
  disabled?: boolean;
}) {
  const secrets = useSecrets(projectId);
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const stored = secrets.data?.secrets.find((s) => s.name === name);
  const unavailable = secrets.data && !secrets.data.available;

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!value.trim()) return;
    setBusy(true);
    try {
      await api.setSecret(projectId, name, value);
      setValue("");
      setEditing(false);
      toast({ tone: "success", title: `${label}: сохранено`, text: "Значение больше не показывается — только последние символы." });
    } catch (err) {
      toast({ tone: "danger", title: `${label}: не сохранено`, text: errorText(err) });
    } finally {
      setBusy(false);
      void queryClient.invalidateQueries({ queryKey: secretsKey(projectId) });
    }
  }

  async function remove() {
    if (!window.confirm(`Удалить «${label}»? Интеграция перестанет работать, пока не будет задано новое значение.`)) return;
    setBusy(true);
    try {
      await api.deleteSecret(projectId, name);
      toast({ tone: "success", title: `${label}: удалено` });
    } catch (err) {
      toast({ tone: "danger", title: `${label}: не удалено`, text: errorText(err) });
    } finally {
      setBusy(false);
      void queryClient.invalidateQueries({ queryKey: secretsKey(projectId) });
    }
  }

  return (
    <div className="secret-field">
      <span className="field-label">{label}</span>
      {unavailable ? (
        <p className="text-muted secret-note">
          Сервер не хранит секреты: администратору нужно задать <code>AI_CODING_SERVER_SECRET_KEY</code>.
        </p>
      ) : stored && !editing ? (
        <div className="row">
          <Badge tone="success">
            <KeyRound size={11} aria-hidden /> задан {stored.hint}
          </Badge>
          <span className="text-muted secret-note">
            {stored.updated_by}, {relativeTime(stored.updated_at)}
          </span>
          <Button size="sm" disabled={disabled || busy} onClick={() => setEditing(true)}>
            Заменить
          </Button>
          <Button size="sm" variant="ghost" icon={<Trash2 size={13} />} disabled={disabled || busy} onClick={() => void remove()} aria-label={`Удалить: ${label}`} />
        </div>
      ) : (
        <form className="row" onSubmit={(e) => void save(e)}>
          <input
            className="input secret-input"
            type="password"
            autoComplete="off"
            spellCheck={false}
            aria-label={label}
            placeholder={placeholder ?? "Значение"}
            value={value}
            disabled={disabled || busy}
            onChange={(e) => setValue(e.target.value)}
          />
          <Button type="submit" size="sm" variant="primary" loading={busy} disabled={disabled || !value.trim()}>
            Сохранить
          </Button>
          {stored && (
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Отмена
            </Button>
          )}
        </form>
      )}
      {help && <span className="text-muted secret-note">{help}</span>}
    </div>
  );
}
