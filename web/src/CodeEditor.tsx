import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { Compartment, EditorState, type Extension, StateField } from "@codemirror/state";
import { oneDark } from "@codemirror/theme-one-dark";
import { Decoration, EditorView, keymap, showTooltip, type Tooltip, WidgetType } from "@codemirror/view";
import { basicSetup } from "codemirror";
import { useEffect, useRef } from "react";
import { ASK_LABELS, type AskKind, type InlineHunk } from "./review/inline";

// CodeMirror 6: styles go through adoptedStyleSheets (allowed by the strict CSP); the
// grammar of the file's language is loaded on demand (a separate chunk per language).
// AI in the editor: the agent's pending changes are marked in place, each with «Принять» /
// «Отклонить»; a selection offers AI actions (Ctrl+I) that go to the workspace's chat.

export interface Selection {
  from: number; // first line, 1-based
  to: number; // last line
  text: string;
}

type Handlers = {
  onChange: (text: string) => void;
  onSave: () => void;
  onHunk?: (index: number, action: "accept" | "reject") => void;
  onAsk?: (kind: AskKind, selection: Selection) => void;
};

const AI_THEME = EditorView.baseTheme({
  // A line opened from elsewhere (a failed test, a finding): revealed and marked.
  ".cm-target-line": {
    backgroundColor: "color-mix(in srgb, var(--info, #60a5fa) 20%, transparent)",
    boxShadow: "inset 3px 0 0 var(--info, #60a5fa)",
  },
  ".cm-ai-added": { backgroundColor: "color-mix(in srgb, var(--success, #34d399) 15%, transparent)" },
  ".cm-ai-hunk": {
    margin: "4px 0",
    padding: "4px 8px",
    borderLeft: "3px solid var(--warning, #fbbf24)",
    background: "color-mix(in srgb, var(--warning, #fbbf24) 8%, transparent)",
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: "12px",
  },
  ".cm-ai-hunk-head": { display: "flex", alignItems: "center", gap: "6px" },
  ".cm-ai-hunk-head span": { flex: "1", color: "var(--text-secondary, #9aa4b5)" },
  ".cm-ai-hunk button, .cm-ai-actions button": {
    font: "500 12px var(--font-sans, system-ui)",
    padding: "2px 8px",
    borderRadius: "5px",
    border: "1px solid var(--border-secondary, #343c4d)",
    background: "var(--bg-tertiary, #161b24)",
    color: "var(--text-primary, #f3f5f9)",
    cursor: "pointer",
  },
  ".cm-ai-hunk button.cm-ai-accept": { borderColor: "var(--success, #34d399)", color: "var(--success, #34d399)" },
  ".cm-ai-hunk button.cm-ai-reject": { borderColor: "var(--danger, #fb7185)", color: "var(--danger, #fb7185)" },
  ".cm-ai-removed": {
    margin: "2px 0 0",
    padding: "0 4px",
    whiteSpace: "pre-wrap",
    fontFamily: "var(--font-mono, monospace)",
    textDecoration: "line-through",
    color: "var(--danger, #fb7185)",
    background: "color-mix(in srgb, var(--danger, #fb7185) 10%, transparent)",
  },
  ".cm-tooltip.cm-ai-actions": {
    display: "flex",
    gap: "4px",
    padding: "4px",
    borderRadius: "8px",
    border: "1px solid var(--border-secondary, #343c4d)",
    background: "var(--bg-elevated, #1c2230)",
    boxShadow: "0 8px 24px rgb(0 0 0 / 35%)",
  },
  ".cm-ai-actions button:hover, .cm-ai-hunk button:hover": { borderColor: "var(--accent-primary, #8b7cff)" },
});

function button(label: string, aria: string, className: string, onClick: () => void): HTMLButtonElement {
  const el = document.createElement("button");
  el.type = "button";
  el.textContent = label;
  el.className = className;
  el.setAttribute("aria-label", aria);
  el.addEventListener("mousedown", (e) => e.preventDefault()); // keep the editor's selection
  el.addEventListener("click", onClick);
  return el;
}

class HunkWidget extends WidgetType {
  constructor(
    readonly hunk: InlineHunk,
    readonly handlers: { current: Handlers },
  ) {
    super();
  }

  eq(other: HunkWidget): boolean {
    return other.hunk.index === this.hunk.index && other.hunk.added.join() === this.hunk.added.join() && other.hunk.removed.join("\n") === this.hunk.removed.join("\n");
  }

  toDOM(): HTMLElement {
    const box = document.createElement("div");
    box.className = "cm-ai-hunk";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", `Изменение ${this.hunk.index + 1}`);
    const head = document.createElement("div");
    head.className = "cm-ai-hunk-head";
    const text = document.createElement("span");
    text.textContent = `Изменение · −${this.hunk.removed.length} +${this.hunk.added.length}`;
    const n = this.hunk.index;
    head.append(
      text,
      button("Принять", `Принять изменение ${n + 1}`, "cm-ai-accept", () => this.handlers.current.onHunk?.(n, "accept")),
      button("Отклонить", `Отклонить изменение ${n + 1}`, "cm-ai-reject", () => this.handlers.current.onHunk?.(n, "reject")),
    );
    box.append(head);
    for (const line of this.hunk.removed.slice(0, 30)) {
      const pre = document.createElement("pre");
      pre.className = "cm-ai-removed";
      pre.textContent = line || " ";
      box.append(pre);
    }
    return box;
  }

  ignoreEvent(): boolean {
    return true;
  }
}

function hunkMarks(marks: InlineHunk[], handlers: { current: Handlers }): Extension {
  if (!marks.length) return [];
  return EditorView.decorations.compute(["doc"], (state) => {
    const ranges = [];
    for (const m of marks) {
      if (m.anchor <= state.doc.lines) {
        ranges.push(Decoration.widget({ widget: new HunkWidget(m, handlers), block: true, side: -1 }).range(state.doc.line(m.anchor).from));
      }
      for (const n of m.added) if (n <= state.doc.lines) ranges.push(Decoration.line({ class: "cm-ai-added" }).range(state.doc.line(n).from));
    }
    return Decoration.set(ranges, true);
  });
}

function selectionOf(state: EditorState): Selection | null {
  const s = state.selection.main;
  if (s.empty) return null;
  const from = state.doc.lineAt(s.from).number;
  const to = state.doc.lineAt(s.to > s.from ? s.to - 1 : s.to).number;
  return { from, to, text: state.sliceDoc(s.from, s.to) };
}

function aiActions(handlers: { current: Handlers }): Extension {
  const tooltips = (state: EditorState): readonly Tooltip[] => {
    const s = state.selection.main;
    if (s.empty) return [];
    return [
      {
        pos: s.head,
        above: s.head === s.from,
        strictSide: false,
        arrow: false,
        create: (view) => {
          const dom = document.createElement("div");
          dom.className = "cm-ai-actions";
          dom.setAttribute("role", "toolbar");
          dom.setAttribute("aria-label", "ИИ для выделенного кода");
          for (const kind of Object.keys(ASK_LABELS) as AskKind[]) {
            dom.append(
              button(ASK_LABELS[kind], `ИИ: ${ASK_LABELS[kind]}`, `cm-ai-${kind}`, () => {
                const selection = selectionOf(view.state);
                if (selection) handlers.current.onAsk?.(kind, selection);
              }),
            );
          }
          return { dom };
        },
      },
    ];
  };
  const field = StateField.define<readonly Tooltip[]>({
    create: tooltips,
    update: (value, tr) => (tr.docChanged || tr.selection ? tooltips(tr.state) : value),
    provide: (f) => showTooltip.computeN([f], (state) => state.field(f)),
  });
  const ask = keymap.of([
    {
      key: "Mod-i",
      preventDefault: true,
      run: (view) => {
        let selection = selectionOf(view.state);
        if (!selection) {
          const line = view.state.doc.lineAt(view.state.selection.main.head);
          selection = { from: line.number, to: line.number, text: line.text };
        }
        handlers.current.onAsk?.("ask", selection);
        return true;
      },
    },
  ]);
  return [field, ask];
}

export default function CodeEditor({
  path,
  content,
  readOnly,
  onChange,
  onSave,
  marks = [],
  onHunk,
  onAsk,
  line,
  lineKey,
}: {
  path: string;
  content: string;
  readOnly: boolean;
  onChange: (text: string) => void;
  onSave: () => void;
  /** The agent's pending changes in this file (empty while the file has unsaved edits). */
  marks?: InlineHunk[];
  onHunk?: (index: number, action: "accept" | "reject") => void;
  /** AI actions on a selection; without it (read-only project files) there are none. */
  onAsk?: (kind: AskKind, selection: Selection) => void;
  /** A 1-based line to reveal and mark (opened at a line: a failed test, a finding). */
  line?: number;
  /** Changes when the same line is asked for again. */
  lineKey?: number;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const handlers = useRef<Handlers>({ onChange, onSave, onHunk, onAsk });
  handlers.current = { onChange, onSave, onHunk, onAsk };
  const marksCompartment = useRef(new Compartment());
  const lineCompartment = useRef(new Compartment());
  const withAi = Boolean(onAsk) && !readOnly;

  useEffect(() => {
    if (!host.current) return undefined;
    const language = new Compartment();
    const dark = (document.documentElement.dataset.theme ?? "obsidian") !== "arctic";
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: content,
        extensions: [
          basicSetup,
          keymap.of([{ key: "Mod-s", preventDefault: true, run: () => (handlers.current.onSave(), true) }]),
          language.of([]),
          EditorState.readOnly.of(readOnly),
          EditorView.editable.of(!readOnly),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) handlers.current.onChange(update.state.doc.toString());
          }),
          AI_THEME,
          marksCompartment.current.of([]),
          lineCompartment.current.of([]),
          ...(withAi ? [aiActions(handlers)] : []),
          ...(dark ? [oneDark] : []),
        ],
      }),
    });
    view.current = editor;
    let alive = true;
    const description = LanguageDescription.matchFilename(languages, path);
    void description?.load().then((support) => {
      if (alive) editor.dispatch({ effects: language.reconfigure(support) });
    });
    return () => {
      alive = false;
      view.current = null;
      editor.destroy();
    };
    // A new document (another file, or the same file reloaded) gets a new editor.
  }, [path, content, readOnly, withAi]);

  // The marks follow the review (they arrive after the file, and vanish on unsaved edits).
  const key = marks.map((m) => `${m.index}:${m.anchor}:${m.added.join(",")}:${m.removed.length}`).join("|");
  useEffect(() => {
    view.current?.dispatch({ effects: marksCompartment.current.reconfigure(hunkMarks(marks, handlers)) });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` summarises `marks`
  }, [key, path, content]);

  // Opened at a line: put the cursor there, scroll it to the middle, mark it.
  useEffect(() => {
    const editor = view.current;
    if (!editor || !line) return;
    const number = Math.max(1, Math.min(line, editor.state.doc.lines));
    const at = editor.state.doc.line(number).from;
    editor.dispatch({
      selection: { anchor: at },
      effects: [
        EditorView.scrollIntoView(at, { y: "center" }),
        lineCompartment.current.reconfigure(EditorView.decorations.of(Decoration.set([Decoration.line({ class: "cm-target-line" }).range(at)]))),
      ],
    });
  }, [line, lineKey, path, content]);

  return <div className="code-editor" ref={host} />;
}
