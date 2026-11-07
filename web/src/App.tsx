import { QueryClientProvider } from "@tanstack/react-query";
import { lazy, type ReactNode, Suspense, useCallback, useEffect, useState } from "react";
import { Account } from "./Account";
import { ApiError, api, getToken, setToken, type Me } from "./api";
import { queryClient } from "./data/queries";
import { Login } from "./Login";
import { AppShell, useThemeEffect } from "./shell/AppShell";
import { parseRoute as parse, type Route } from "./shell/routes";
import { useUi } from "./shell/store";
import { Skeleton } from "./ui";
import { Users } from "./Users";

// NEXUS Developer OS: the App Shell and its sections. Heavy sections load on demand.
const Dashboard = lazy(() => import("./pages/Dashboard"));
const ProjectList = lazy(() => import("./ProjectList").then((m) => ({ default: m.ProjectList })));
const ProjectPage = lazy(() => import("./ProjectPage").then((m) => ({ default: m.ProjectPage })));
const NewProjectPage = lazy(() => import("./pages/NewProjectPage").then((m) => ({ default: m.NewProjectPage })));
const RunPage = lazy(() => import("./RunPage").then((m) => ({ default: m.RunPage })));
const Workspace = lazy(() => import("./Workspace").then((m) => ({ default: m.Workspace })));
const Launcher = lazy(() => import("./pages/Launcher"));
const Tasks = lazy(() => import("./pages/Tasks"));
const Agents = lazy(() => import("./pages/Agents"));
const AuditCenter = lazy(() => import("./pages/AuditCenter"));
const SecurityCenter = lazy(() => import("./pages/SecurityCenter"));
const Testing = lazy(() => import("./pages/Testing"));
const Deployments = lazy(() => import("./pages/Deployments"));
const Models = lazy(() => import("./pages/Models"));
const Infrastructure = lazy(() => import("./pages/Infrastructure"));
const Settings = lazy(() => import("./pages/Settings"));

export const parseRoute = parse;

function Page({ route, me, onLogout }: { route: Route; me: Me; onLogout: () => void }): ReactNode {
  switch (route.view) {
    case "dashboard":
      return <Dashboard me={me} />;
    case "workspace":
      return <Launcher target="ai" />;
    case "studio":
      return <Launcher target="code" />;
    case "projects":
      return <ProjectList me={me} />;
    case "project":
      return <ProjectPage id={route.id ?? ""} me={me} />;
    case "new":
      return <NewProjectPage />;
    case "ai":
      return <Workspace id={route.id ?? ""} layout="ai" query={route.query ?? {}} />;
    case "code":
      return <Workspace id={route.id ?? ""} layout="code" query={route.query ?? {}} />;
    case "run":
      return <RunPage id={route.id ?? ""} />;
    case "tasks":
      return <Tasks query={route.query ?? {}} />;
    case "agents":
      return <Agents query={route.query ?? {}} />;
    case "audit":
      return <AuditCenter query={route.query ?? {}} />;
    case "security":
      // The page itself refuses to load anything when the server has the module off.
      return <SecurityCenter me={me} query={route.query ?? {}} />;
    case "testing":
      return <Testing />;
    case "deployments":
      return <Deployments />;
    case "models":
      return <Models />;
    case "infrastructure":
      return <Infrastructure me={me} />;
    case "settings":
      return <Settings me={me} tab={route.tab} onLogout={onLogout} />;
    case "users":
      return me.is_admin ? <Users me={me} /> : <p className="error">Раздел доступен только администраторам.</p>;
    case "account":
      return <Account me={me} onLogout={onLogout} />;
  }
}

/** Remembers the project the user works in (for the Copilot, AI Workspace, filters). */
function useFollowProject(route: Route): void {
  const setProject = useUi((s) => s.setProject);
  useEffect(() => {
    if (route.id && ["project", "ai", "code"].includes(route.view)) setProject(route.id);
  }, [route, setProject]);
}

function Authenticated({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useFollowProject(route);
  return (
    <AppShell route={route} me={me} onLogout={onLogout}>
      <Suspense fallback={<Skeleton lines={6} />}>
        <Page route={route} me={me} onLogout={onLogout} />
      </Suspense>
    </AppShell>
  );
}

function Anonymous({ onLogin }: { onLogin: (me: Me) => void }) {
  useThemeEffect();
  return <Login onLogin={onLogin} />;
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState<boolean>(() => getToken() !== null);

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
    queryClient.clear();
    window.location.hash = "#/dashboard";
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      {loading ? (
        <div className="boot" aria-busy="true">
          <Skeleton lines={3} />
        </div>
      ) : me ? (
        <Authenticated me={me} onLogout={logout} />
      ) : (
        <Anonymous onLogin={setMe} />
      )}
    </QueryClientProvider>
  );
}
