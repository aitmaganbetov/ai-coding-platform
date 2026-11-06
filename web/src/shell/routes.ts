import {
  Bot,
  BrainCircuit,
  Code2,
  FlaskConical,
  FolderGit2,
  LayoutDashboard,
  ListChecks,
  type LucideIcon,
  Rocket,
  Server,
  Settings,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

// Hash routing of NEXUS. Old addresses keep working (#/projects, #/runs/<id>, #/users,
// #/account, #/projects/<id>/code): links in chats, reports and bookmarks point at them.

export type View =
  | "dashboard"
  | "workspace"
  | "ai"
  | "projects"
  | "project"
  | "new"
  | "agents"
  | "tasks"
  | "run"
  | "studio"
  | "code"
  | "audit"
  | "testing"
  | "deployments"
  | "models"
  | "infrastructure"
  | "settings"
  | "users"
  | "account";

export interface Route {
  view: View;
  id?: string;
  tab?: string;
  query?: Record<string, string>;
}

export function parseRoute(hash: string): Route {
  const [path = "", search = ""] = hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const params = Object.fromEntries(new URLSearchParams(search));
  const query = Object.keys(params).length > 0 ? params : undefined;
  const route = (r: Route): Route => (query ? { ...r, query } : r);
  const [head, id, tab] = parts;
  switch (head) {
    case undefined:
    case "dashboard":
      return route({ view: "dashboard" });
    case "projects":
      if (id === "new") return route({ view: "new" });
      if (id && tab === "code") return route({ view: "code", id });
      if (id && tab === "ai") return route({ view: "ai", id });
      if (id) return route(tab ? { view: "project", id, tab } : { view: "project", id });
      return route({ view: "projects" });
    case "runs":
      return id ? route({ view: "run", id }) : route({ view: "tasks" });
    case "settings":
      return route(id ? { view: "settings", tab: id } : { view: "settings" });
    case "new":
      return route({ view: "new" });
    case "workspace":
    case "agents":
    case "tasks":
    case "studio":
    case "audit":
    case "testing":
    case "deployments":
    case "models":
    case "infrastructure":
    case "users":
    case "account":
      return route({ view: head });
    default:
      return { view: "dashboard" };
  }
}

export function href(view: View, id?: string, query?: Record<string, string | undefined>): string {
  const base: Record<View, string> = {
    dashboard: "#/dashboard",
    workspace: "#/workspace",
    ai: `#/projects/${id ?? ""}/ai`,
    projects: "#/projects",
    project: `#/projects/${id ?? ""}`,
    new: "#/projects/new",
    agents: "#/agents",
    tasks: "#/tasks",
    run: `#/runs/${id ?? ""}`,
    studio: "#/studio",
    code: `#/projects/${id ?? ""}/code`,
    audit: "#/audit",
    testing: "#/testing",
    deployments: "#/deployments",
    models: "#/models",
    infrastructure: "#/infrastructure",
    settings: id ? `#/settings/${id}` : "#/settings",
    users: "#/settings/users",
    account: "#/settings/account",
  };
  const params = Object.entries(query ?? {}).filter((e): e is [string, string] => Boolean(e[1]));
  return params.length ? `${base[view]}?${new URLSearchParams(params).toString()}` : base[view];
}

export interface NavItem {
  id: string;
  label: string;
  icon: LucideIcon;
  href: string;
  views: View[];
  adminOnly?: boolean;
  group: "work" | "quality" | "platform";
}

export const NAV: NavItem[] = [
  { id: "dashboard", label: "Mission Control", icon: LayoutDashboard, href: href("dashboard"), views: ["dashboard"], group: "work" },
  { id: "workspace", label: "AI Workspace", icon: Sparkles, href: href("workspace"), views: ["workspace", "ai"], group: "work" },
  { id: "projects", label: "Проекты", icon: FolderGit2, href: href("projects"), views: ["projects", "project", "new"], group: "work" },
  { id: "tasks", label: "Задачи", icon: ListChecks, href: href("tasks"), views: ["tasks", "run"], group: "work" },
  { id: "agents", label: "Агенты", icon: Bot, href: href("agents"), views: ["agents"], group: "work" },
  { id: "studio", label: "Code Studio", icon: Code2, href: href("studio"), views: ["studio", "code"], group: "work" },
  { id: "audit", label: "Audit Center", icon: ShieldCheck, href: href("audit"), views: ["audit"], group: "quality" },
  { id: "testing", label: "Тесты и Gates", icon: FlaskConical, href: href("testing"), views: ["testing"], group: "quality" },
  { id: "deployments", label: "Деплой", icon: Rocket, href: href("deployments"), views: ["deployments"], group: "platform" },
  { id: "models", label: "Модели", icon: BrainCircuit, href: href("models"), views: ["models"], group: "platform" },
  { id: "infrastructure", label: "Инфраструктура", icon: Server, href: href("infrastructure"), views: ["infrastructure"], group: "platform" },
  { id: "settings", label: "Настройки", icon: Settings, href: href("settings"), views: ["settings", "users", "account"], group: "platform" },
];

export const GROUPS: Record<NavItem["group"], string> = {
  work: "Разработка",
  quality: "Качество",
  platform: "Платформа",
};

export function navFor(view: View): NavItem | undefined {
  return NAV.find((item) => item.views.includes(view));
}
