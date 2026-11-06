import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DiffFile } from "../api";
import { ChatPanel } from "../ChatPanel";
import { askPrompt, inlineHunks, useChatBridge } from "./inline";

const FILE: DiffFile = {
  path: "app/shop.py",
  old_path: null,
  status: "modified",
  binary: false,
  header: "",
  additions: 3,
  deletions: 2,
  hunks: [
    {
      header: "@@ -1,4 +1,4 @@",
      section: "",
      old_start: 1,
      old_lines: 4,
      new_start: 1,
      new_lines: 4,
      patch: "",
      lines: [
        { kind: "context", text: "a" },
        { kind: "del", text: "b" },
        { kind: "add", text: "B" },
        { kind: "context", text: "c" },
        { kind: "context", text: "d" },
      ],
    },
    {
      header: "@@ -20,3 +20,4 @@",
      section: "",
      old_start: 20,
      old_lines: 3,
      new_start: 20,
      new_lines: 4,
      patch: "",
      lines: [
        { kind: "context", text: "x" },
        { kind: "add", text: "y1" },
        { kind: "add", text: "y2" },
        { kind: "del", text: "z" },
      ],
    },
    {
      header: "@@ -40,2 +41,1 @@",
      section: "",
      old_start: 40,
      old_lines: 2,
      new_start: 41,
      new_lines: 1,
      patch: "",
      lines: [
        { kind: "context", text: "k" },
        { kind: "del", text: "gone" },
      ],
    },
  ],
};

// The chat daemon's socket, as in app.test.tsx.
class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.last = this;
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.();
    }, 0);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close() {
    this.readyState = 3;
  }
  emit(event: Record<string, unknown>) {
    act(() => this.onmessage?.({ data: JSON.stringify(event) }));
  }
}

beforeEach(() => {
  sessionStorage.setItem("ai-coding-token", "aic_session");
  vi.stubGlobal("WebSocket", FakeSocket);
  useChatBridge.setState({ request: null });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AI in the editor", () => {
  it("places each pending change on the lines of the working file", () => {
    const marks = inlineHunks(FILE, 60);
    expect(marks.map((m) => [m.index, m.anchor, m.added, m.removed])).toEqual([
      [0, 2, [2], ["b"]],
      [1, 21, [21, 22], ["z"]],
      [2, 42, [], ["gone"]], // a pure deletion sits where the lines were
    ]);
    expect(inlineHunks(FILE, 30)[2]?.anchor).toBe(30); // past the end: the last line
    expect(inlineHunks({ ...FILE, binary: true }, 60)).toEqual([]);
    expect(inlineHunks(undefined, 60)).toEqual([]);
  });

  it("asks the agent with the file, the lines and the code", () => {
    const fix = askPrompt("fix", "app/shop.py", 4, 9, "def total(items):\n    return 0\n");
    expect(fix).toContain("`app/shop.py` (строки 4–9)");
    expect(fix).toContain("```python\ndef total(items):\n    return 0\n```");
    expect(fix).toContain("Внеси изменения в файл");
    expect(askPrompt("explain", "web/App.tsx", 3, 3, "x")).toMatch(/^Объясни.*строка 3.*Ничего не меняй/s);
    expect(askPrompt("ask", "a.go", 1, 2, "x")).toContain("```go\nx\n```");
  });

  it("sends an editor action through the open chat, or leaves a question in the box", async () => {
    render(<ChatPanel projectId="p1" onOpenTerminal={() => undefined} onFilesChanged={() => undefined} />);
    await waitFor(() => expect(FakeSocket.last?.readyState).toBe(1));
    const socket = FakeSocket.last as FakeSocket;
    socket.emit({ t: "hello", sessions: [] });
    socket.emit({ t: "opened", meta: { id: "s1", agent: "claude", mode: "ask", workflow: "develop", title: "Чат" }, history: [] });

    act(() => useChatBridge.getState().ask("p1", "Найди и исправь ошибки", true));
    await waitFor(() => expect(socket.sent.at(-1)).toEqual({ op: "send", session: "s1", text: "Найди и исправь ошибки", mode: "ask" }));
    expect(useChatBridge.getState().request).toBeNull();

    act(() => useChatBridge.getState().ask("p1", "Вопрос по коду", false));
    await waitFor(() => expect((screen.getByLabelText("Сообщение") as HTMLTextAreaElement).value).toBe("Вопрос по коду"));
    expect(socket.sent.at(-1)?.text).toBe("Найди и исправь ошибки"); // not sent

    act(() => useChatBridge.getState().ask("other-project", "чужое", true));
    expect(useChatBridge.getState().request?.projectId).toBe("other-project"); // left alone
  });
});
