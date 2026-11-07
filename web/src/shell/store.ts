import { create } from "zustand";

// Per-viewer UI preferences (theme, layout, favourites, current project). Stored in
// localStorage as a convenience only: every read and write may fail (private windows,
// blocked storage) and the interface then simply starts from the defaults.

export type ThemeChoice = "system" | "obsidian" | "arctic" | "oled";

/** An open workspace (a tab above the main area): a project in one of its two views. */
export interface OpenWorkspace {
  id: string;
  layout: "ai" | "code";
  name: string;
}
const MAX_WORKSPACES = 8;

export interface UiState {
  theme: ThemeChoice;
  accent: string | null;
  sidebarCollapsed: boolean;
  mobileNavOpen: boolean;
  favourites: string[];
  projectId: string | null;
  copilotOpen: boolean;
  paletteOpen: boolean;
  workspaces: OpenWorkspace[];
  fullscreen: boolean;
  setTheme: (theme: ThemeChoice) => void;
  setAccent: (accent: string | null) => void;
  toggleSidebar: () => void;
  setMobileNav: (open: boolean) => void;
  toggleFavourite: (id: string) => void;
  setProject: (id: string | null) => void;
  setCopilot: (open: boolean) => void;
  setPalette: (open: boolean) => void;
  /** A workspace was opened (or used again): first in the tabs, at most 8 kept. */
  openWorkspace: (ws: OpenWorkspace) => void;
  closeWorkspace: (id: string, layout: OpenWorkspace["layout"]) => void;
  setFullscreen: (on: boolean) => void;
}

const KEY = "nexus-ui";
const PERSISTED = ["theme", "accent", "sidebarCollapsed", "favourites", "projectId", "copilotOpen", "workspaces"] as const;

function load(): Partial<UiState> {
  try {
    const raw = window.localStorage.getItem(KEY);
    const data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    const out: Partial<UiState> = {};
    if (["system", "obsidian", "arctic", "oled"].includes(String(data.theme))) out.theme = data.theme as ThemeChoice;
    if (typeof data.accent === "string" && /^#[0-9a-f]{6}$/i.test(data.accent)) out.accent = data.accent;
    if (typeof data.sidebarCollapsed === "boolean") out.sidebarCollapsed = data.sidebarCollapsed;
    if (Array.isArray(data.favourites)) out.favourites = data.favourites.filter((f): f is string => typeof f === "string");
    if (typeof data.projectId === "string") out.projectId = data.projectId;
    if (typeof data.copilotOpen === "boolean") out.copilotOpen = data.copilotOpen;
    if (Array.isArray(data.workspaces))
      out.workspaces = data.workspaces
        .filter((w): w is OpenWorkspace => {
          const ws = w as Partial<OpenWorkspace> | null;
          return Boolean(ws) && typeof ws?.id === "string" && (ws?.layout === "ai" || ws?.layout === "code") && typeof ws?.name === "string";
        })
        .slice(0, MAX_WORKSPACES);
    return out;
  } catch {
    return {};
  }
}

function save(state: UiState): void {
  try {
    const data = Object.fromEntries(PERSISTED.map((k) => [k, state[k]]));
    window.localStorage.setItem(KEY, JSON.stringify(data));
  } catch {
    /* storage unavailable: preferences last for this page only */
  }
}

export const useUi = create<UiState>((set) => ({
  theme: "system",
  accent: null,
  sidebarCollapsed: false,
  mobileNavOpen: false,
  favourites: ["dashboard", "workspace", "tasks"],
  projectId: null,
  copilotOpen: false,
  paletteOpen: false,
  workspaces: [],
  fullscreen: false,
  ...load(),
  setTheme: (theme) => set({ theme }),
  setAccent: (accent) => set({ accent }),
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  setMobileNav: (mobileNavOpen) => set({ mobileNavOpen }),
  toggleFavourite: (id) =>
    set((s) => ({ favourites: s.favourites.includes(id) ? s.favourites.filter((f) => f !== id) : [...s.favourites, id] })),
  setProject: (projectId) => set({ projectId }),
  setCopilot: (copilotOpen) => set({ copilotOpen }),
  setPalette: (paletteOpen) => set({ paletteOpen }),
  openWorkspace: (ws) =>
    set((s) => {
      const rest = s.workspaces.filter((w) => !(w.id === ws.id && w.layout === ws.layout));
      return { workspaces: [ws, ...rest].slice(0, MAX_WORKSPACES) };
    }),
  closeWorkspace: (id, layout) => set((s) => ({ workspaces: s.workspaces.filter((w) => !(w.id === id && w.layout === layout)) })),
  setFullscreen: (fullscreen) => set({ fullscreen }),
}));

useUi.subscribe(save);

/** The theme actually shown: "system" follows the operating system. */
export function resolveTheme(choice: ThemeChoice, prefersDark: boolean): Exclude<ThemeChoice, "system"> {
  if (choice !== "system") return choice;
  return prefersDark ? "obsidian" : "arctic";
}

export const ACCENTS = ["#8b7cff", "#45d6e8", "#34d399", "#fbbf24", "#fb7185", "#60a5fa", "#f472b6"];
