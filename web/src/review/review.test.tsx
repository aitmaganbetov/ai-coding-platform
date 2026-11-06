import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DiffFile, WorkspaceDiff } from "../api";
import { queryClient } from "../data/queries";
import { Toaster, TooltipProvider } from "../ui";
import { ChangesReview, filePatch, splitRows, unifiedRows } from "./ChangesReview";
import { QueryClientProvider } from "@tanstack/react-query";

const HEADER = "diff --git a/app.py b/app.py\nindex 1..2 100644\n--- a/app.py\n+++ b/app.py\n";
const HUNK1 = "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n";
const HUNK2 = "@@ -10,2 +10,3 @@ def f():\n x\n+y\n z\n";

const APP: DiffFile = {
  path: "app.py",
  old_path: null,
  status: "modified",
  binary: false,
  header: HEADER,
  additions: 2,
  deletions: 1,
  hunks: [
    {
      header: "@@ -1,3 +1,3 @@",
      section: "",
      old_start: 1,
      old_lines: 3,
      new_start: 1,
      new_lines: 3,
      patch: HEADER + HUNK1,
      lines: [
        { kind: "context", text: "a" },
        { kind: "del", text: "b" },
        { kind: "add", text: "B" },
        { kind: "context", text: "c" },
      ],
    },
    {
      header: "@@ -10,2 +10,3 @@ def f():",
      section: "def f():",
      old_start: 10,
      old_lines: 2,
      new_start: 10,
      new_lines: 3,
      patch: HEADER + HUNK2,
      lines: [
        { kind: "context", text: "x" },
        { kind: "add", text: "y" },
        { kind: "context", text: "z" },
      ],
    },
  ],
};
const NEW: DiffFile = { ...APP, path: "docs/new.md", status: "added", additions: 1, deletions: 0, header: "", hunks: [] };

type Call = { method: string; path: string; body: unknown };

function serve(diffs: WorkspaceDiff[], review: (body: Record<string, unknown>) => number = () => 200) {
  const calls: Call[] = [];
  let index = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace("/api/v1", "");
      const method = init.method ?? "GET";
      const body = typeof init.body === "string" ? (JSON.parse(init.body) as Record<string, unknown>) : undefined;
      calls.push({ method, path, body });
      if (method === "GET" && path === "/projects/p1/workspace/diff") {
        const data = diffs[Math.min(index, diffs.length - 1)];
        return new Response(JSON.stringify(data), { status: 200 });
      }
      if (path === "/projects/p1/workspace/review") {
        const status = review(body ?? {});
        index += 1;
        return new Response(JSON.stringify(status === 200 ? { ok: true, output: "" } : { error: { message: "patch does not apply" } }), { status });
      }
      if (path === "/projects/p1/workspace/commit") {
        index += 1;
        return new Response(JSON.stringify({ ok: true, output: "", branch: "main" }), { status: 200 });
      }
      return new Response(JSON.stringify({ error: { message: "no route" } }), { status: 404 });
    }),
  );
  return calls;
}

function show(onChanged = vi.fn()) {
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChangesReview projectId="p1" onChanged={onChanged} />
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return onChanged;
}

beforeEach(() => {
  queryClient.clear();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("diff rows", () => {
  it("pairs deletions with additions side by side and numbers both sides", () => {
    const rows = splitRows(APP.hunks[0]!);
    expect(rows.map(([l, r]) => [l.no, l.kind, r.no, r.kind])).toEqual([
      [1, "context", 1, "context"],
      [2, "del", 2, "add"],
      [3, "context", 3, "context"],
    ]);
    const unified = unifiedRows(APP.hunks[1]!);
    expect(unified.map((r) => [r.old, r.new, r.kind])).toEqual([
      [10, 10, "context"],
      [null, 11, "add"],
      [11, 12, "context"],
    ]);
    expect(filePatch(APP)).toBe(HEADER + HUNK1 + HUNK2);
  });
});

describe("AI Changes Review", () => {
  it("accepts and rejects hunks, a new file, then commits what was accepted", async () => {
    const accepted: WorkspaceDiff = { accepted: [{ ...APP, hunks: [APP.hunks[0]!] }], pending: [], new_files: [], truncated: false };
    const calls = serve([{ accepted: [], pending: [APP], new_files: [NEW], truncated: false }, accepted]);
    const onChanged = show();
    expect(await screen.findByText(/На проверке: 3 · принято: 0/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Принять фрагмент 1" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ action: "accept", patch: HEADER + HUNK1 }));
    expect(await screen.findByText(/На проверке: 0 · принято: 1/)).toBeTruthy();
    expect(onChanged).not.toHaveBeenCalled(); // accepting changes no file

    // Back to review sends the accepted hunk back.
    fireEvent.click(screen.getByRole("button", { name: "Вернуть фрагмент 1 на проверку" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST").at(-1)?.body).toEqual({ action: "unstage", patch: HEADER + HUNK1 }));
    fireEvent.change(screen.getByLabelText("Сообщение коммита принятых изменений"), { target: { value: "reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать принятое" }));
    await waitFor(() => expect(calls.find((c) => c.path.endsWith("/commit"))?.body).toEqual({ message: "reviewed", staged_only: true }));
  });

  it("rejects a whole file and a new file, reloading the files", async () => {
    const calls = serve([{ accepted: [], pending: [APP], new_files: [NEW], truncated: false }]);
    const onChanged = show();
    const card = await screen.findByRole("article", { name: "app.py: изменён" });
    fireEvent.click(within(card).getByRole("button", { name: "Отклонить файл" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ action: "reject", patch: HEADER + HUNK1 + HUNK2 }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fresh = await screen.findByRole("article", { name: "docs/new.md: новый" });
    fireEvent.click(within(fresh).getByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST").at(-1)?.body).toEqual({ action: "reject_file", path: "docs/new.md" }));
  });

  it("shows side by side and says when a hunk no longer applies", async () => {
    serve([{ accepted: [], pending: [APP], new_files: [], truncated: false }], () => 409);
    show();
    await screen.findByText(/На проверке: 2/);
    fireEvent.click(screen.getByRole("radio", { name: "Рядом" }));
    const table = screen.getAllByRole("table", { name: /Фрагмент @@ -1,3/ })[0]!;
    expect(table.className).toContain("split");
    expect(localStorage.getItem("nexus-review-mode")).toBe("split");
    fireEvent.click(screen.getByRole("button", { name: "Отклонить фрагмент 2" }));
    expect(await screen.findByText("Изменение не применено")).toBeTruthy();
    expect(screen.getByText(/patch does not apply/)).toBeTruthy();
  });

  it("says when there is nothing to review", async () => {
    serve([{ accepted: [], pending: [], new_files: [], truncated: false }]);
    show();
    expect(await screen.findByText("Изменений нет")).toBeTruthy();
  });
});
