import { create } from "zustand";

// Per-viewer UI preferences (theme, layout, favourites, current project). Stored in
// localStorage as a convenience only: every read and write may fail (private windows,
// blocked storage) and the interface then simply starts from the defaults.

export type ThemeChoice = "system" | "obsidian" | "arctic" | "oled";

export interface UiState {
  theme: ThemeChoice;
  accent: string | null;
  sidebarCollapsed: boolean;
  mobileNavOpen: boolean;
  favourites: string[];
  projectId: string | null;
  copilotOpen: boolean;
  paletteOpen: boolean;
  setTheme: (theme: ThemeChoice) => void;
  setAccent: (accent: string | null) => void;
  toggleSidebar: () => void;
  setMobileNav: (open: boolean) => void;
  toggleFavourite: (id: string) => void;
  setProject: (id: string | null) => void;
  setCopilot: (open: boolean) => void;
  setPalette: (open: boolean) => void;
}

const KEY = "nexus-ui";
const PERSISTED = ["theme", "accent", "sidebarCollapsed", "favourites", "projectId", "copilotOpen"] as const;

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
}));

useUi.subscribe(save);

/** The theme actually shown: "system" follows the operating system. */
export function resolveTheme(choice: ThemeChoice, prefersDark: boolean): Exclude<ThemeChoice, "system"> {
  if (choice !== "system") return choice;
  return prefersDark ? "obsidian" : "arctic";
}

export const ACCENTS = ["#8b7cff", "#45d6e8", "#34d399", "#fbbf24", "#fb7185", "#60a5fa", "#f472b6"];
