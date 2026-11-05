import { useCallback, useEffect, useState } from "react";
import { ApiError, api, getToken, setToken, type Me } from "./api";
import { Login } from "./Login";
import { ProjectList } from "./ProjectList";
import { ProjectPage } from "./ProjectPage";
import { RunPage } from "./RunPage";

// Minimal hash routing: #/projects, #/projects/<id>, #/runs/<id>.
type Route = { view: "projects" } | { view: "project"; id: string } | { view: "run"; id: string };

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "projects" && parts[1]) return { view: "project", id: parts[1] };
  if (parts[0] === "runs" && parts[1]) return { view: "run", id: parts[1] };
  return { view: "projects" };
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [loading, setLoading] = useState<boolean>(() => getToken() !== null);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!getToken()) return;
    api
      .me()
      .then(setMe)
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 401) setToken(null);
      })
      .finally(() => setLoading(false));
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setMe(null);
    window.location.hash = "#/projects";
  }, []);

  if (loading) return <p className="muted">Загрузка…</p>;
  if (!me) return <Login onLogin={setMe} />;
  return (
    <div className="app">
      <header className="topbar">
        <a href="#/projects" className="brand">
          AI Coding Platform
        </a>
        <span className="spacer" />
        <span className="muted">
          {me.username}
          {me.is_admin ? " (администратор)" : ""}
        </span>
        <button type="button" onClick={logout}>
          Выйти
        </button>
      </header>
      <main>
        {route.view === "projects" && <ProjectList />}
        {route.view === "project" && <ProjectPage id={route.id} />}
        {route.view === "run" && <RunPage id={route.id} />}
      </main>
    </div>
  );
}
