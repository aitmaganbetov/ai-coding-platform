import {
  Bell,
  Bot,
  ChevronRight,
  LogOut,
  Menu as MenuIcon,
  Monitor,
  Moon,
  Palette,
  Search,
  Settings,
  Sun,
  User,
  Zap,
} from "lucide-react";
import type { Me } from "../api";
import { useProject, useReadiness, useRun, useRuns } from "../data/queries";
import { ACCENTS, type ThemeChoice, useUi } from "./store";
import { Badge, cx, DropdownMenu, isActiveRun, Kbd, MenuItem, MenuLabel, MenuSeparator, StatusDot, Tooltip } from "../ui";
import { href, navFor, type Route } from "./routes";

const THEMES: { value: ThemeChoice; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "Как в системе", icon: Monitor },
  { value: "obsidian", label: "Obsidian Dark", icon: Moon },
  { value: "arctic", label: "Arctic Light", icon: Sun },
  { value: "oled", label: "Midnight OLED", icon: Moon },
];

function Breadcrumbs({ route }: { route: Route }) {
  const section = navFor(route.view);
  const withProject = ["project", "ai", "code"].includes(route.view) ? route.id : undefined;
  const project = useProject(withProject);
  const run = useRun(route.view === "run" ? route.id : undefined);
  const runProject = useProject(run.data?.project_id);
  const crumbs: { label: string; href?: string }[] = [];
  if (section) crumbs.push({ label: section.label, href: section.href });
  if (withProject) crumbs.push({ label: project.data?.name ?? "Проект", href: href("project", withProject) });
  if (route.view === "ai") crumbs.push({ label: "AI Workspace" });
  if (route.view === "code") crumbs.push({ label: "Code Studio" });
  if (route.view === "new") crumbs.push({ label: "Новый проект" });
  if (route.view === "run" && route.id) {
    if (runProject.data) crumbs.push({ label: runProject.data.name, href: href("project", runProject.data.id) });
    crumbs.push({ label: `Run ${route.id.slice(-8)}` });
  }
  return (
    <nav className="breadcrumbs" aria-label="Навигационная цепочка">
      <ol>
        {crumbs.map((c, i) => (
          <li key={`${c.label}-${i}`}>
            {i > 0 && <ChevronRight size={13} aria-hidden className="crumb-sep" />}
            {c.href && i < crumbs.length - 1 ? (
              <a href={c.href} className="truncate">
                {c.label}
              </a>
            ) : (
              <span className="truncate" aria-current={i === crumbs.length - 1 ? "page" : undefined}>
                {c.label}
              </span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

function ServerStatus() {
  const ready = useReadiness();
  const ok = ready.data === true;
  const label = ready.isLoading ? "Сервер: проверка" : ok ? "Сервер доступен" : "Сервер или база данных недоступны";
  return (
    <Tooltip label={label}>
      <span className="bar-chip" tabIndex={0}>
        <StatusDot tone={ready.isLoading ? "neutral" : ok ? "success" : "danger"} label={label} pulse={!ok && !ready.isLoading} />
        <span className="bar-chip-text">Сервер</span>
      </span>
    </Tooltip>
  );
}

function AiStatus() {
  const projectId = useUi((s) => s.projectId);
  const project = useProject(projectId);
  const ai = project.data?.config?.ai;
  const text = !projectId ? "AI: проект не выбран" : !ai ? "AI: нет данных" : ai.enabled ? `AI: ${ai.provider}${ai.model ? ` · ${ai.model}` : ""}` : "AI оркестратора: выключен";
  const tone = !ai ? "neutral" : ai.enabled ? "success" : "warning";
  return (
    <Tooltip label={`${text}. Чат Claude Code / Codex работает на вашей подписке независимо от этого.`}>
      <a className="bar-chip" href={href("models")}>
        <StatusDot tone={tone} label={text} />
        <span className="bar-chip-text truncate">{ai?.enabled ? ai.provider : "AI"}</span>
      </a>
    </Tooltip>
  );
}

function ActiveTasks() {
  const runs = useRuns();
  const active = (runs.data ?? []).filter((r) => isActiveRun(r.state));
  return (
    <Tooltip label={active.length ? `Активных задач: ${active.length}` : "Нет активных задач"}>
      <a className="bar-chip" href={href("tasks", undefined, { state: "active" })}>
        <Zap size={14} aria-hidden className={cx(active.length > 0 && "tone-accent")} />
        <span className="bar-chip-text">{active.length}</span>
        <span className="sr-only">активных задач</span>
      </a>
    </Tooltip>
  );
}

function Notifications() {
  const runs = useRuns();
  const waiting = (runs.data ?? []).filter((r) => r.state === "AWAITING_APPROVAL");
  const trigger = (
    <button type="button" className="bar-icon" aria-label={`Уведомления${waiting.length ? `: ${waiting.length} ждут согласия` : ""}`}>
      <Bell size={16} aria-hidden />
      {waiting.length > 0 && <span className="bar-dot" aria-hidden />}
    </button>
  );
  return (
    <DropdownMenu trigger={trigger}>
      <MenuLabel>Ждут вашего согласия</MenuLabel>
      {waiting.length === 0 && <div className="menu-empty">Ничего не ждёт согласия</div>}
      {waiting.slice(0, 8).map((r) => (
        <MenuItem key={r.id} onSelect={() => (window.location.hash = href("run", r.id))}>
          <span className="menu-two-line">
            <span className="truncate">{r.goal || r.kind}</span>
            <small className="text-muted">{r.project_name}</small>
          </span>
        </MenuItem>
      ))}
      <MenuSeparator />
      <MenuItem onSelect={() => (window.location.hash = href("tasks", undefined, { tab: "approvals" }))}>Очередь согласований</MenuItem>
    </DropdownMenu>
  );
}

function ThemeMenu() {
  const { theme, setTheme, accent, setAccent } = useUi();
  const trigger = (
    <button type="button" className="bar-icon" aria-label="Тема оформления">
      <Palette size={16} aria-hidden />
    </button>
  );
  return (
    <DropdownMenu trigger={trigger}>
      <MenuLabel>Тема</MenuLabel>
      {THEMES.map(({ value, label, icon: Icon }) => (
        <MenuItem key={value} onSelect={() => setTheme(value)} icon={<Icon size={14} />}>
          {label}
          {theme === value && <Badge tone="accent">выбрана</Badge>}
        </MenuItem>
      ))}
      <MenuSeparator />
      <MenuLabel>Акцент</MenuLabel>
      <div className="accent-row" role="group" aria-label="Акцентный цвет">
        {ACCENTS.map((c) => (
          <button
            key={c}
            type="button"
            className={cx("swatch", (accent ?? ACCENTS[0]) === c && "on")}
            style={{ background: c }}
            aria-label={`Акцент ${c}`}
            aria-pressed={(accent ?? ACCENTS[0]) === c}
            onClick={() => setAccent(c === ACCENTS[0] ? null : c)}
          />
        ))}
        <label className="swatch custom" aria-label="Свой цвет" title="Свой цвет">
          <input type="color" value={accent ?? ACCENTS[0]} onChange={(e) => setAccent(e.target.value)} />
        </label>
      </div>
    </DropdownMenu>
  );
}

function Profile({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const trigger = (
    <button type="button" className="avatar" aria-label={`Профиль: ${me.username}`}>
      {me.username.slice(0, 2).toUpperCase()}
    </button>
  );
  return (
    <DropdownMenu trigger={trigger}>
      <MenuLabel>
        {me.username}
        {me.is_admin ? " · администратор" : ""}
      </MenuLabel>
      <MenuItem icon={<User size={14} />} onSelect={() => (window.location.hash = href("account"))}>
        Профиль и токены
      </MenuItem>
      <MenuItem icon={<Settings size={14} />} onSelect={() => (window.location.hash = href("settings"))}>
        Настройки
      </MenuItem>
      <MenuSeparator />
      <MenuItem icon={<LogOut size={14} />} onSelect={onLogout} danger>
        Выйти
      </MenuItem>
    </DropdownMenu>
  );
}

export function CommandBar({ route, me, onLogout }: { route: Route; me: Me; onLogout: () => void }) {
  const { setPalette, copilotOpen, setCopilot, setMobileNav } = useUi();
  const embedsChat = route.view === "ai" || route.view === "code";
  return (
    <header className="commandbar">
      <button type="button" className="bar-icon mobile-only" aria-label="Открыть меню" onClick={() => setMobileNav(true)}>
        <MenuIcon size={18} aria-hidden />
      </button>
      <Breadcrumbs route={route} />
      <button type="button" className="search-trigger" onClick={() => setPalette(true)} aria-label="Поиск и команды" aria-keyshortcuts="Control+K Meta+K">
        <Search size={14} aria-hidden />
        <span className="search-trigger-text">Поиск и команды</span>
        <span className="search-trigger-keys" aria-hidden>
          <Kbd>Ctrl</Kbd>
          <Kbd>K</Kbd>
        </span>
      </button>
      <div className="bar-status">
        <ServerStatus />
        <AiStatus />
        <ActiveTasks />
      </div>
      <Notifications />
      <ThemeMenu />
      {!embedsChat && (
        <Tooltip label={copilotOpen ? "Скрыть AI Copilot" : "Открыть AI Copilot"}>
          <button type="button" className={cx("bar-icon", copilotOpen && "on")} aria-label="AI Copilot" aria-pressed={copilotOpen} onClick={() => setCopilot(!copilotOpen)}>
            <Bot size={16} aria-hidden />
          </button>
        </Tooltip>
      )}
      <Profile me={me} onLogout={onLogout} />
    </header>
  );
}
