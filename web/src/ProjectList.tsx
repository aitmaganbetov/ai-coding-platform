import { useEffect, useState } from "react";
import { api, type Project } from "./api";

export function ProjectList() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.projects().then(setProjects, (err: Error) => setError(err.message));
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!projects) return <p className="muted">Загрузка…</p>;
  return (
    <section>
      <h2>Проекты</h2>
      {projects.length === 0 ? (
        <p className="muted">Нет проектов, доступных вам.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Проект</th>
              <th>Роль</th>
              <th>Создан</th>
            </tr>
          </thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.id}>
                <td>
                  {p.role ? <a href={`#/projects/${p.id}`}>{p.name}</a> : p.name}
                </td>
                <td>{p.role ?? "—"}</td>
                <td>{p.created_at.slice(0, 19).replace("T", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
