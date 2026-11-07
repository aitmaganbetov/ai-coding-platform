import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { queryClient } from "./data/queries";
import { closeTab, EMPTY_LAYOUT, merge, moveTab, openTab, restoreLayout, saveLayout, split } from "./editorTabs";
import { useUi } from "./shell/store";

// Editor tabs, Split View, open at a line and the workspace tabs (ADR-0045). CodeMirror
// needs a real layout engine: the editor is a textarea here, its props are recorded.
const editors = vi.hoisted(() => [] as { path: string; content: string; line?: number }[]);
vi.mock("./CodeEditor", () => ({
  default: (props: { path: string; content: string; readOnly: boolean; line?: number; onChange: (t: string) => void }) => {
    editors.push({ path: props.path, content: props.content, line: props.line });
    return <textarea aria-label={`editor ${props.path}`} defaultValue={props.content} readOnly={props.readOnly} onChange={(e) => props.onChange(e.target.value)} />;
  },
}));
vi.mock("./TerminalPanel", () => ({ default: () => <div>terminal</div> }));

class FakeSocket {
  static OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  send() {}
  close() {}
}

const PROJECT = { id: "p1", name: "shop", role: "developer", created_at: "2026-11-05T10:00:00", members: [], kinds: [] };
const FILES: Record<string, string> = { "app.py": "x = 1\n", "lib.py": "def f():\n    return 1\n", "src/app.py": "a\nb\nc\nd\ne\nf\ng\nh\n" };

function serve() {
  const calls: { method: string; path: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace("/api/v1", "");
      const method = init.method ?? "GET";
      calls.push({ method, path });
      const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
      if (path === "/me") return json({ username: "alice", is_admin: false });
      if (path === "/projects") return json([PROJECT]);
      if (path === "/projects/p1") return json(PROJECT);
      if (path === "/projects/p1/workspace")
        return json({ git: true, exists: true, can_edit: true, terminal: "on", branch_prefix: "ws/alice/" });
      if (path === "/projects/p1/files?path=&scope=workspace")
        return json([
          { name: "src", type: "dir", size: 0 },
          { name: "app.py", type: "file", size: 6 },
          { name: "lib.py", type: "file", size: 20 },
        ]);
      const file = /^\/projects\/p1\/file\?path=([^&]+)&scope=workspace$/.exec(path);
      if (file) {
        const name = decodeURIComponent(file[1] ?? "");
        const content = FILES[name];
        return content === undefined
          ? json({ error: { message: "not found" } }, 404)
          : json({ path: name, size: content.length, content, binary: false, too_large: false, version: "v1" });
      }
      if (path === "/projects/p1/workspace/git") return json({ branch: "main", files: [] });
      return json({ error: { message: `no route ${path}` } }, 404);
    }),
  );
  return calls;
}

function signedIn(hash: string) {
  sessionStorage.setItem("ai-coding-token", "aic_session");
  window.location.hash = hash;
}

// The Workspace is a lazy route: its first appearance may take a while under load.
const FIRST = { timeout: 5000 };
const tab = (name: string) => screen.getByRole("tab", { name: new RegExp(`^${name.replace(".", "\\.")}`) });

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  queryClient.clear();
  editors.length = 0;
  useUi.setState({ projectId: null, copilotOpen: false, workspaces: [], fullscreen: false });
  window.location.hash = "";
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("tab and group rules", () => {
  it("opens, moves, splits, merges and closes tabs; a file lives in one group", () => {
    let layout = openTab(openTab(EMPTY_LAYOUT, "a.py"), "b.py");
    expect(layout.groups).toEqual([{ tabs: ["a.py", "b.py"], active: "b.py" }]);
    layout = split(layout); // the active tab goes right
    expect(layout).toEqual({ groups: [{ tabs: ["a.py"], active: "a.py" }, { tabs: ["b.py"], active: "b.py" }], focus: 1 });
    expect(openTab(layout, "a.py").focus).toBe(0); // already open on the left: shown there
    layout = openTab(layout, "c.py"); // new files open in the focused group
    expect(layout.groups[1]).toEqual({ tabs: ["b.py", "c.py"], active: "c.py" });
    layout = moveTab(layout, "c.py");
    expect(layout.groups.map((g) => g.tabs)).toEqual([["a.py", "c.py"], ["b.py"]]);
    layout = closeTab(layout, "b.py"); // the emptied right group goes away
    expect(layout.groups.map((g) => g.tabs)).toEqual([["a.py", "c.py"]]);
    expect(merge(split(openTab(layout, "d.py"))).groups.map((g) => g.tabs)).toEqual([["a.py", "c.py", "d.py"]]);
    expect(split(EMPTY_LAYOUT).groups).toHaveLength(2); // an empty right group to open into
  });

  it("keeps the tabs of a project in this browser, paths only", () => {
    const layout = split(openTab(openTab(EMPTY_LAYOUT, "a.py"), "b.py"));
    saveLayout("p9", layout);
    expect(restoreLayout("p9")).toEqual(layout);
    localStorage.setItem("nexus-tabs-p8", "{not json");
    expect(restoreLayout("p8")).toEqual(EMPTY_LAYOUT);
    localStorage.setItem("nexus-tabs-p7", JSON.stringify({ groups: [{ tabs: ["x", 3, ""], active: "y" }], focus: 5 }));
    expect(restoreLayout("p7")).toEqual({ groups: [{ tabs: ["x"], active: "x" }], focus: 0 });
  });
});

describe("Code Studio tabs", () => {
  it("opens files as tabs, marks unsaved ones and asks before closing them", async () => {
    signedIn("#/projects/p1/code");
    serve();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "app.py" }, FIRST));
    expect(await screen.findByLabelText("editor app.py")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "lib.py" }));
    expect(await screen.findByLabelText("editor lib.py")).toBeTruthy();
    expect(tab("lib.py").getAttribute("aria-selected")).toBe("true");
    fireEvent.click(tab("app.py"));
    expect(tab("app.py").getAttribute("aria-selected")).toBe("true");
    fireEvent.change(screen.getByLabelText("editor app.py"), { target: { value: "x = 2\n" } });
    expect(within(tab("app.py")).getByRole("img", { name: "есть несохранённые изменения" })).toBeTruthy();

    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(screen.getByRole("button", { name: "Закрыть app.py" }));
    expect(confirm).toHaveBeenCalled();
    expect(tab("app.py")).toBeTruthy(); // kept
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Закрыть app.py" }));
    await waitFor(() => expect(screen.queryByRole("tab", { name: /^app\.py/ })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Закрыть lib.py" })); // nothing unsaved: no question
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(JSON.parse(localStorage.getItem("nexus-tabs-p1") ?? "{}").groups[0].tabs).toEqual([]);
  });

  it("restores the project's tabs and loads the shown one", async () => {
    localStorage.setItem("nexus-tabs-p1", JSON.stringify({ groups: [{ tabs: ["app.py", "lib.py"], active: "lib.py" }], focus: 0 }));
    signedIn("#/projects/p1/code");
    const calls = serve();
    render(<App />);
    expect(await screen.findByLabelText("editor lib.py", {}, FIRST)).toBeTruthy();
    expect(tab("app.py").getAttribute("aria-selected")).toBe("false");
    expect(calls.some((c) => c.path.includes("file?path=app.py"))).toBe(false); // loaded when shown
    fireEvent.click(tab("app.py"));
    expect(await screen.findByLabelText("editor app.py")).toBeTruthy();
  });

  it("splits the editor: the active file opens on the right, with its unsaved text", async () => {
    signedIn("#/projects/p1/code");
    serve();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "app.py" }, FIRST));
    await screen.findByLabelText("editor app.py");
    fireEvent.click(screen.getByRole("button", { name: "lib.py" }));
    fireEvent.change(await screen.findByLabelText("editor lib.py"), { target: { value: "changed\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Разделить редактор" }));
    const right = await screen.findByRole("region", { name: "Правая группа редактора" });
    expect(within(right).getByLabelText("editor lib.py")).toBeTruthy();
    expect(editors.filter((e) => e.path === "lib.py").at(-1)?.content).toBe("changed\n"); // moved with its text
    const left = screen.getByRole("region", { name: "Левая группа редактора" });
    expect(within(left).getByLabelText("editor app.py")).toBeTruthy();
    fireEvent.click(within(right).getByRole("button", { name: "Открыть слева" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Правая группа редактора" })).toBeNull());
  });

  it("keeps unsaved text when the editor leaves the screen (AI Workspace: Chat and back)", async () => {
    signedIn("#/projects/p1/ai");
    serve();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "app.py" }, FIRST));
    fireEvent.change(await screen.findByLabelText("editor app.py"), { target: { value: "x = 42\n" } });
    fireEvent.click(screen.getByRole("tab", { name: /^Chat/ }));
    await waitFor(() => expect(screen.queryByLabelText("editor app.py")).toBeNull());
    fireEvent.click(screen.getByRole("tab", { name: "Code" }));
    expect(((await screen.findByLabelText("editor app.py")) as HTMLTextAreaElement).value).toBe("x = 42\n");
    expect(within(tab("app.py")).getByRole("img", { name: "есть несохранённые изменения" })).toBeTruthy();
  });

  it("opens a file at a line from a link", async () => {
    signedIn("#/projects/p1/code?file=src%2Fapp.py&line=7");
    serve();
    render(<App />);
    expect(await screen.findByLabelText("editor src/app.py", {}, FIRST)).toBeTruthy();
    expect(editors.filter((e) => e.path === "src/app.py").at(-1)?.line).toBe(7);
  });
});

describe("workspace tabs", () => {
  it("lists the open workspaces, switches and closes them", async () => {
    useUi.setState({ workspaces: [{ id: "p2", layout: "ai", name: "billing" }] });
    signedIn("#/projects/p1/code");
    serve();
    render(<App />);
    const strip = await screen.findByRole("navigation", { name: "Открытые рабочие области" }, FIRST);
    await waitFor(() => expect(within(strip).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["#/projects/p1/code", "#/projects/p2/ai"]));
    expect(within(strip).getByRole("link", { current: "page" }).textContent).toContain("shop");
    fireEvent.click(within(strip).getByRole("button", { name: "Закрыть вкладку billing — AI Workspace" }));
    expect(useUi.getState().workspaces.map((w) => w.id)).toEqual(["p1"]);
    fireEvent.click(within(strip).getByRole("button", { name: "Закрыть вкладку shop — Code Studio" }));
    await waitFor(() => expect(window.location.hash).toBe("#/studio")); // the current one: back to the list
  });

  it("goes full screen and leaves it with Esc", async () => {
    signedIn("#/projects/p1/code");
    serve();
    const { container } = render(<App />);
    await screen.findByRole("button", { name: "app.py" }, FIRST);
    fireEvent.click(screen.getByRole("button", { name: "Во весь экран" }));
    expect(container.querySelector(".shell")?.className).toContain("fullscreen");
    expect(screen.queryByRole("navigation", { name: "Открытые рабочие области" })).toBeNull();
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(container.querySelector(".shell")?.className).not.toContain("fullscreen");
  });
});
