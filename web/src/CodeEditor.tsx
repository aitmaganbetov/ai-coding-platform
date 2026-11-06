import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { Compartment, EditorState } from "@codemirror/state";
import { oneDark } from "@codemirror/theme-one-dark";
import { EditorView, keymap } from "@codemirror/view";
import { basicSetup } from "codemirror";
import { useEffect, useRef } from "react";

// CodeMirror 6: styles go through adoptedStyleSheets (allowed by the strict CSP); the
// grammar of the file's language is loaded on demand (a separate chunk per language).
export default function CodeEditor({
  path,
  content,
  readOnly,
  onChange,
  onSave,
}: {
  path: string;
  content: string;
  readOnly: boolean;
  onChange: (text: string) => void;
  onSave: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const handlers = useRef({ onChange, onSave });
  handlers.current = { onChange, onSave };

  useEffect(() => {
    if (!host.current) return undefined;
    const language = new Compartment();
    const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
    const view = new EditorView({
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
          ...(dark ? [oneDark] : []),
        ],
      }),
    });
    let alive = true;
    const description = LanguageDescription.matchFilename(languages, path);
    void description?.load().then((support) => {
      if (alive) view.dispatch({ effects: language.reconfigure(support) });
    });
    return () => {
      alive = false;
      view.destroy();
    };
    // A new document (another file, or the same file reloaded) gets a new editor.
  }, [path, content, readOnly]);

  return <div className="code-editor" ref={host} />;
}
