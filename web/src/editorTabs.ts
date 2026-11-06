// Editor tabs and groups of a workspace (Split View, ADR-0045): pure functions over the
// layout, so the rules are tested apart from the UI.
//
// A file is open in at most one group: «Открыть справа» moves its tab (with its unsaved
// text) instead of duplicating it — two editable copies of one file could diverge.

export interface EditorGroup {
  tabs: string[];
  active: string | null;
}

export interface EditorLayout {
  groups: EditorGroup[]; // one or two
  focus: number; // the group new files open in
}

export const EMPTY_LAYOUT: EditorLayout = { groups: [{ tabs: [], active: null }], focus: 0 };
const MAX_TABS = 20;

export function groupOf(layout: EditorLayout, path: string): number {
  return layout.groups.findIndex((g) => g.tabs.includes(path));
}

/** Open (or show) a file: in the group that has it, else in ``group`` (default: focus). */
export function openTab(layout: EditorLayout, path: string, group?: number): EditorLayout {
  const existing = groupOf(layout, path);
  if (existing >= 0) return activate(layout, existing, path);
  const target = Math.min(group ?? layout.focus, layout.groups.length - 1);
  const groups = layout.groups.map((g, i) => {
    if (i !== target) return g;
    const at = g.active ? g.tabs.indexOf(g.active) + 1 : g.tabs.length;
    const tabs = [...g.tabs.slice(0, at), path, ...g.tabs.slice(at)].slice(-MAX_TABS);
    return { tabs, active: path };
  });
  return { groups, focus: target };
}

export function activate(layout: EditorLayout, group: number, path: string): EditorLayout {
  return {
    groups: layout.groups.map((g, i) => (i === group ? { ...g, active: path } : g)),
    focus: group,
  };
}

/** Close a tab; an emptied second group goes away, an emptied first one takes the second. */
export function closeTab(layout: EditorLayout, path: string): EditorLayout {
  const index = groupOf(layout, path);
  if (index < 0) return layout;
  const groups = layout.groups.map((g, i) => {
    if (i !== index) return g;
    const at = g.tabs.indexOf(path);
    const tabs = g.tabs.filter((t) => t !== path);
    const active = g.active === path ? (tabs[Math.min(at, tabs.length - 1)] ?? null) : g.active;
    return { tabs, active };
  });
  return collapse({ groups, focus: layout.focus });
}

/** Move a tab to the other group (creating it): «Открыть справа» / «Открыть слева». */
export function moveTab(layout: EditorLayout, path: string): EditorLayout {
  const from = groupOf(layout, path);
  if (from < 0) return layout;
  const to = from === 0 ? 1 : 0;
  const groups = [...layout.groups.map((g) => ({ ...g })), ...(layout.groups.length < 2 ? [{ tabs: [], active: null }] : [])];
  const source = groups[from] as EditorGroup;
  const at = source.tabs.indexOf(path);
  source.tabs = source.tabs.filter((t) => t !== path);
  if (source.active === path) source.active = source.tabs[Math.min(at, source.tabs.length - 1)] ?? null;
  const target = groups[to] as EditorGroup;
  target.tabs = [...target.tabs, path];
  target.active = path;
  return collapse({ groups, focus: to }, to);
}

/** Split: a second group, taking the active tab when the first keeps another one. */
export function split(layout: EditorLayout): EditorLayout {
  if (layout.groups.length > 1) return layout;
  const first = layout.groups[0] as EditorGroup;
  if (first.active && first.tabs.length > 1) return moveTab(layout, first.active);
  return { groups: [first, { tabs: [], active: null }], focus: 1 };
}

/** Back to one group: the second group's tabs join the first. */
export function merge(layout: EditorLayout): EditorLayout {
  if (layout.groups.length < 2) return layout;
  const [first, second] = layout.groups as [EditorGroup, EditorGroup];
  const tabs = [...first.tabs, ...second.tabs.filter((t) => !first.tabs.includes(t))];
  return { groups: [{ tabs, active: first.active ?? second.active }], focus: 0 };
}

function collapse(layout: EditorLayout, keep?: number): EditorLayout {
  if (layout.groups.length < 2) return layout;
  const [first, second] = layout.groups as [EditorGroup, EditorGroup];
  if (second.tabs.length === 0 && keep !== 1) return { groups: [first], focus: 0 };
  if (first.tabs.length === 0 && keep !== 0) return { groups: [second], focus: 0 };
  return layout;
}

/** The tabs of a project, kept in this browser (paths only, never contents). */
export function restoreLayout(projectId: string): EditorLayout {
  try {
    const raw = window.localStorage.getItem(`nexus-tabs-${projectId}`);
    const data = raw ? (JSON.parse(raw) as Partial<EditorLayout>) : null;
    const groups = (Array.isArray(data?.groups) ? data.groups : [])
      .slice(0, 2)
      .map((g) => {
        const tabs = (Array.isArray(g?.tabs) ? g.tabs : []).filter((t): t is string => typeof t === "string" && t.length > 0).slice(0, MAX_TABS);
        return { tabs, active: typeof g?.active === "string" && tabs.includes(g.active) ? g.active : (tabs[0] ?? null) };
      })
      .filter((g) => g.tabs.length > 0);
    if (groups.length === 0) return EMPTY_LAYOUT;
    return { groups, focus: Math.min(Number(data?.focus) || 0, groups.length - 1) };
  } catch {
    return EMPTY_LAYOUT;
  }
}

export function saveLayout(projectId: string, layout: EditorLayout): void {
  try {
    window.localStorage.setItem(`nexus-tabs-${projectId}`, JSON.stringify(layout));
  } catch {
    /* the tabs last for this page only */
  }
}
