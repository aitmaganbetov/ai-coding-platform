import { create } from "zustand";
import type { DiffFile } from "../api";

// AI in the editor: where the agent's pending changes are in the open file (to mark them
// and accept or reject each in place), and the requests a selection sends to the chat.

export interface InlineHunk {
  /** The hunk's index in the file's pending diff (what the review endpoint gets). */
  index: number;
  /** 1-based lines of the working-tree file the hunk added or changed. */
  added: number[];
  /** Lines the hunk removed (shown struck through above the change). */
  removed: string[];
  /** The line the hunk's widget sits on (first change; past the end: the last line). */
  anchor: number;
}

export function inlineHunks(file: DiffFile | undefined, lineCount: number): InlineHunk[] {
  if (!file || file.binary) return [];
  return file.hunks.map((hunk, index) => {
    let line = hunk.new_start;
    let anchor: number | null = null;
    const added: number[] = [];
    const removed: string[] = [];
    for (const l of hunk.lines) {
      if (l.kind === "add") {
        anchor ??= line;
        added.push(line++);
      } else if (l.kind === "del") {
        anchor ??= line;
        removed.push(l.text);
      } else if (l.kind === "context") {
        line++;
      }
    }
    const at = Math.max(1, Math.min(anchor ?? hunk.new_start, Math.max(1, lineCount)));
    return { index, added, removed, anchor: at };
  });
}

export type AskKind = "explain" | "fix" | "improve" | "tests" | "ask";

export const ASK_LABELS: Record<AskKind, string> = {
  explain: "Объяснить",
  fix: "Исправить",
  improve: "Улучшить",
  tests: "Тесты",
  ask: "Спросить…",
};

const FENCE: Record<string, string> = { py: "python", ts: "ts", tsx: "tsx", js: "js", jsx: "jsx", go: "go", rs: "rust", php: "php", java: "java", rb: "ruby", sh: "bash", md: "markdown", json: "json", yml: "yaml", yaml: "yaml", sql: "sql", css: "css", html: "html" };

/** The chat message of an AI action on a selection (Russian: the agent answers in it). */
export function askPrompt(kind: AskKind, path: string, from: number, to: number, code: string): string {
  const lines = from === to ? `строка ${from}` : `строки ${from}–${to}`;
  const fence = FENCE[path.split(".").pop()?.toLowerCase() ?? ""] ?? "";
  const block = `\`\`\`${fence}\n${code.replace(/\n$/, "")}\n\`\`\``;
  const where = `\`${path}\` (${lines})`;
  switch (kind) {
    case "explain":
      return `Объясни, что делает этот код в ${where}. Ничего не меняй.\n${block}`;
    case "fix":
      return `Найди и исправь ошибки в коде ${where}. Внеси изменения в файл — я проверю их прямо в редакторе.\n${block}`;
    case "improve":
      return `Улучши код ${where}: читаемость, надёжность и производительность, не меняя поведения. Внеси изменения в файл — я проверю их в редакторе.\n${block}`;
    case "tests":
      return `Напиши тесты для кода ${where} в стиле тестов проекта, добавь их в подходящий файл и запусти.\n${block}`;
    case "ask":
      return `Вопрос по коду ${where}:\n${block}\n`;
  }
}

/** Editor → chat: a message to send (or to put in the box) in the project's chat. */
export interface ChatRequest {
  id: number;
  projectId: string;
  text: string;
  send: boolean;
}

export const useChatBridge = create<{
  request: ChatRequest | null;
  ask: (projectId: string, text: string, send: boolean) => void;
  done: (id: number) => void;
}>((set) => ({
  request: null,
  ask: (projectId, text, send) => set({ request: { id: Date.now() + Math.random(), projectId, text, send } }),
  done: (id) => set((s) => (s.request?.id === id ? { request: null } : s)),
}));
