import { lazy, type ReactNode, Suspense, useEffect } from "react";
import type { Me } from "../api";
import { Toaster, TooltipProvider } from "../ui";
import { CommandBar } from "./CommandBar";
import { CommandPalette } from "./CommandPalette";
import type { Route } from "./routes";
import { Sidebar } from "./Sidebar";
import { resolveTheme, useUi } from "./store";

const Copilot = lazy(() => import("./Copilot").then((m) => ({ default: m.Copilot })));

/** Applies the chosen theme and accent to <html> (and follows the OS for "system"). */
export function useThemeEffect(): void {
  const theme = useUi((s) => s.theme);
  const accent = useUi((s) => s.accent);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme = resolveTheme(theme, media?.matches ?? true);
    };
    apply();
    media?.addEventListener?.("change", apply);
    return () => media?.removeEventListener?.("change", apply);
  }, [theme]);
  useEffect(() => {
    const style = document.documentElement.style;
    if (accent) {
      style.setProperty("--accent-primary", accent);
      style.setProperty("--accent-hover", `color-mix(in srgb, ${accent} 78%, white)`);
    } else {
      style.removeProperty("--accent-primary");
      style.removeProperty("--accent-hover");
    }
  }, [accent]);
}

export function AppShell({ route, me, onLogout, children }: { route: Route; me: Me; onLogout: () => void; children: ReactNode }) {
  useThemeEffect();
  const { copilotOpen, sidebarCollapsed, setMobileNav } = useUi();
  const embedsChat = route.view === "ai" || route.view === "code";
  const wide = ["ai", "code"].includes(route.view);
  useEffect(() => setMobileNav(false), [route, setMobileNav]);
  return (
    <TooltipProvider delayDuration={350}>
      <a href="#main" className="skip-link">
        К содержимому
      </a>
      <div className={["shell", sidebarCollapsed && "sidebar-collapsed", copilotOpen && !embedsChat && "with-copilot"].filter(Boolean).join(" ")}>
        <Sidebar route={route} me={me} />
        <CommandBar route={route} me={me} onLogout={onLogout} />
        <main id="main" className={wide ? "shell-main wide" : "shell-main"} tabIndex={-1}>
          {children}
        </main>
        {copilotOpen && !embedsChat && (
          <Suspense fallback={<aside className="copilot" aria-busy="true" />}>
            <Copilot />
          </Suspense>
        )}
      </div>
      <CommandPalette me={me} onLogout={onLogout} />
      <Toaster />
    </TooltipProvider>
  );
}
