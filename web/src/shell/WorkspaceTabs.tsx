import { Code2, Sparkles, X } from "lucide-react";
import { cx } from "../ui";
import { href, type Route } from "./routes";
import { type OpenWorkspace, useUi } from "./store";

const LABEL: Record<OpenWorkspace["layout"], string> = { ai: "AI Workspace", code: "Code Studio" };

/** Tabs of the open workspaces (recently used first), above the main area. */
export function WorkspaceTabs({ route }: { route: Route }) {
  const { workspaces, closeWorkspace } = useUi();
  if (workspaces.length === 0) return null;
  const isCurrent = (w: OpenWorkspace) => route.view === w.layout && route.id === w.id;

  function close(w: OpenWorkspace) {
    const rest = workspaces.filter((x) => x !== w);
    closeWorkspace(w.id, w.layout);
    if (isCurrent(w)) {
      const next = rest[0];
      window.location.hash = next ? href(next.layout, next.id) : href(w.layout === "ai" ? "workspace" : "studio");
    }
  }

  return (
    <nav className="ws-tabs" aria-label="Открытые рабочие области">
      {workspaces.map((w) => {
        const Icon = w.layout === "ai" ? Sparkles : Code2;
        const current = isCurrent(w);
        return (
          <div key={`${w.layout}-${w.id}`} className={cx("ws-tab", current && "current")}>
            <a href={href(w.layout, w.id)} aria-current={current ? "page" : undefined} title={`${w.name} — ${LABEL[w.layout]}`}>
              <Icon size={13} aria-hidden />
              <span className="truncate">{w.name}</span>
              <small>{LABEL[w.layout]}</small>
            </a>
            <button type="button" aria-label={`Закрыть вкладку ${w.name} — ${LABEL[w.layout]}`} onClick={() => close(w)}>
              <X size={12} aria-hidden />
            </button>
          </div>
        );
      })}
    </nav>
  );
}
