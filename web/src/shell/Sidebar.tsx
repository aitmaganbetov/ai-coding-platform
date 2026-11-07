import { Check, ChevronsUpDown, FolderPlus, PanelLeftClose, PanelLeftOpen, Star } from "lucide-react";
import type { Me } from "../api";
import { useProjects } from "../data/queries";
import { cx, DropdownMenu, MenuItem, MenuLabel, MenuSeparator, Tooltip } from "../ui";
import { GROUPS, href, type NavItem, type Route, visibleNav } from "./routes";
import { useUi } from "./store";

export function Logo({ compact }: { compact?: boolean }) {
  return (
    <a href={href("dashboard")} className="brand-mark" aria-label="NEXUS — Mission Control">
      <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden>
        <defs>
          <linearGradient id="nexus-g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="var(--accent-primary)" />
            <stop offset="1" stopColor="var(--accent-cyan)" />
          </linearGradient>
        </defs>
        <path d="M16 2 28.1 9v14L16 30 3.9 23V9Z" fill="none" stroke="url(#nexus-g)" strokeWidth="2.2" />
        <path d="M11 21V11l10 10V11" fill="none" stroke="var(--text-primary)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      {!compact && (
        <span className="brand-text">
          <strong>NEXUS</strong>
          <small>AI Coding Platform</small>
        </span>
      )}
    </a>
  );
}

function ProjectSwitcher({ compact }: { compact: boolean }) {
  const projects = useProjects();
  const { projectId, setProject } = useUi();
  const current = projects.data?.find((p) => p.id === projectId);
  const trigger = (
    <button type="button" className={cx("project-switch", compact && "compact")} aria-label="Текущий проект">
      <span className="project-avatar" aria-hidden>
        {(current?.name ?? "—").slice(0, 2).toUpperCase()}
      </span>
      {!compact && (
        <>
          <span className="project-switch-text">
            <small>Проект</small>
            <span className="truncate">{current?.name ?? "Не выбран"}</span>
          </span>
          <ChevronsUpDown size={14} aria-hidden />
        </>
      )}
    </button>
  );
  return (
    <DropdownMenu trigger={trigger} align="start">
      <MenuLabel>Проекты</MenuLabel>
      {(projects.data ?? []).length === 0 && <MenuLabel>Нет доступных проектов</MenuLabel>}
      {(projects.data ?? []).map((p) => (
        <MenuItem key={p.id} onSelect={() => setProject(p.id)} icon={p.id === projectId ? <Check size={14} /> : <span style={{ width: 14 }} />}>
          <span className="truncate">{p.name}</span>
        </MenuItem>
      ))}
      <MenuSeparator />
      <MenuItem icon={<FolderPlus size={14} />} onSelect={() => (window.location.hash = href("new"))}>
        Подключить проект
      </MenuItem>
    </DropdownMenu>
  );
}

function NavLinkItem({ item, active, compact, favourite, onFavourite }: { item: NavItem; active: boolean; compact: boolean; favourite: boolean; onFavourite?: () => void }) {
  const Icon = item.icon;
  const link = (
    <a href={item.href} className={cx("nav-item", active && "active")} aria-current={active ? "page" : undefined}>
      <Icon size={16} aria-hidden />
      {!compact && <span className="truncate">{item.label}</span>}
    </a>
  );
  return (
    <li className="nav-row">
      {compact ? (
        <Tooltip label={item.label} side="right">
          {link}
        </Tooltip>
      ) : (
        link
      )}
      {!compact && onFavourite && (
        <button
          type="button"
          className={cx("nav-star", favourite && "on")}
          aria-label={favourite ? `Убрать «${item.label}» из избранного` : `Добавить «${item.label}» в избранное`}
          aria-pressed={favourite}
          onClick={onFavourite}
        >
          <Star size={12} aria-hidden />
        </button>
      )}
    </li>
  );
}

export function Sidebar({ route, me }: { route: Route; me: Me }) {
  const { sidebarCollapsed, toggleSidebar, favourites, toggleFavourite, mobileNavOpen, setMobileNav } = useUi();
  const compact = sidebarCollapsed && !mobileNavOpen; // the mobile drawer always shows labels
  const items = visibleNav(me);
  const favs = items.filter((i) => favourites.includes(i.id));
  const isActive = (item: NavItem) => item.views.includes(route.view);
  return (
    <>
      {mobileNavOpen && <div className="sidebar-scrim" onClick={() => setMobileNav(false)} aria-hidden />}
      <aside className={cx("sidebar", compact && "collapsed", mobileNavOpen && "open")} aria-label="Навигация">
        <div className="sidebar-top">
          <Logo compact={compact} />
        </div>
        <div className="sidebar-project">
          <ProjectSwitcher compact={compact} />
        </div>
        <nav className="sidebar-nav" onClick={() => setMobileNav(false)}>
          {favs.length > 0 && (
            <div className="nav-group favs">
              {!compact && <div className="nav-group-label">Избранное</div>}
              <ul>
                {favs.map((item) => (
                  <NavLinkItem key={`fav-${item.id}`} item={item} active={isActive(item)} compact={compact} favourite />
                ))}
              </ul>
            </div>
          )}
          {(Object.keys(GROUPS) as NavItem["group"][]).map((group) => (
            <div key={group} className="nav-group">
              {!compact ? <div className="nav-group-label">{GROUPS[group]}</div> : <div className="nav-group-rule" />}
              <ul>
                {items
                  .filter((i) => i.group === group)
                  .map((item) => (
                    <NavLinkItem
                      key={item.id}
                      item={item}
                      active={isActive(item)}
                      compact={compact}
                      favourite={favourites.includes(item.id)}
                      onFavourite={() => toggleFavourite(item.id)}
                    />
                  ))}
              </ul>
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button type="button" className="nav-item collapse-toggle" onClick={toggleSidebar} aria-label={compact ? "Развернуть меню" : "Свернуть меню"}>
            {compact ? <PanelLeftOpen size={16} aria-hidden /> : <PanelLeftClose size={16} aria-hidden />}
            {!compact && <span>Свернуть</span>}
          </button>
        </div>
      </aside>
    </>
  );
}
