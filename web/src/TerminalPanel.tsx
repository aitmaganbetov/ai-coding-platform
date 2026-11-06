import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { getToken, terminalUrl } from "./api";
import { copyText, findLinks } from "./terminalText";

type State = "connecting" | "open" | "closed";

// The browser terminal of the caller's workspace: tmux in their own container, with the
// official Claude Code and Codex CLIs. Protocol: src/ai_coding/api/terminal.py.
export default function TerminalPanel({ projectId, command }: { projectId: string; command?: string }) {
  const host = useRef<HTMLDivElement>(null);
  const sendInput = useRef<(data: string) => void>(() => undefined);
  const [state, setState] = useState<State>("connecting");
  const [attempt, setAttempt] = useState(0);
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [code, setCode] = useState("");

  useEffect(() => {
    if (!host.current) return undefined;
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      scrollback: 5000,
      macOptionClickForcesSelection: true,
      theme: { background: "#15171b" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon((_event, uri) => window.open(uri, "_blank", "noopener,noreferrer")));
    term.open(host.current);
    fit.fit();
    setState("connecting");
    setLink(null);
    term.write("\x1b[90mПодключение к рабочей области… (первый запуск контейнера занимает до минуты)\x1b[0m\r\n");

    const socket = new WebSocket(terminalUrl(projectId));
    socket.binaryType = "arraybuffer";
    const send = (message: object) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
    };
    sendInput.current = (data: string) => send({ type: "input", data });

    // Ctrl+C copies a selection instead of interrupting; Ctrl+Shift+C always copies;
    // Ctrl+V / Ctrl+Shift+V paste through the browser (xterm.js handles the paste event).
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown" || !(event.ctrlKey || event.metaKey)) return true;
      const key = event.key.toLowerCase();
      if (key === "c" && (event.shiftKey || term.hasSelection())) {
        if (term.hasSelection()) copyText(term.getSelection());
        term.clearSelection();
        event.preventDefault();
        return false;
      }
      if (key === "v") return false;
      return true;
    });

    // The last URL on screen, for the «Открыть» button (sign-in links span several rows).
    let scan = 0;
    const parsed = term.onWriteParsed(() => {
      window.clearTimeout(scan);
      scan = window.setTimeout(() => {
        const buffer = term.buffer.active;
        const rows: string[] = [];
        for (let i = Math.max(0, buffer.length - term.rows - 200); i < buffer.length; i++) {
          rows.push(buffer.getLine(i)?.translateToString(true) ?? "");
        }
        const links = findLinks(rows, term.cols);
        setLink(links.length > 0 ? (links[links.length - 1] ?? null) : null);
      }, 250);
    });

    socket.onopen = () => {
      socket.send(JSON.stringify({ token: getToken(), cols: term.cols, rows: term.rows }));
      setState("open");
      // A command asked for by the page (e.g. "claude auth login" from the chat), typed in
      // once the shell is up.
      if (command) window.setTimeout(() => send({ type: "input", data: `${command}\r` }), 1200);
    };
    socket.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
      if (typeof event.data !== "string") {
        term.write(new Uint8Array(event.data));
        return;
      }
      const control = JSON.parse(event.data) as { type: string; message?: string };
      if (control.type === "error") term.write(`\r\n\x1b[31m${control.message ?? "Ошибка"}\x1b[0m\r\n`);
      if (control.type === "exit") term.write("\r\n\x1b[90m[сеанс завершён]\x1b[0m\r\n");
    };
    socket.onclose = () => setState("closed");
    const input = term.onData((data) => send({ type: "input", data }));
    const resized = term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
    const observer = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* hidden panel */
      }
    });
    observer.observe(host.current);
    term.focus();
    return () => {
      window.clearTimeout(scan);
      observer.disconnect();
      parsed.dispose();
      input.dispose();
      resized.dispose();
      socket.close();
      term.dispose();
    };
  }, [projectId, attempt, command]);

  function copyLink() {
    if (link && copyText(link)) {
      setCopied("Ссылка скопирована");
      window.setTimeout(() => setCopied(null), 2000);
    }
  }

  function pasteCode(event: FormEvent) {
    event.preventDefault();
    if (!code.trim()) return;
    sendInput.current(`${code.trim()}\r`);
    setCode("");
  }

  return (
    <div className="terminal-panel">
      <div className="terminal-bar">
        <span className={`dot dot-${state}`} />
        {state === "open" ? "Терминал" : state === "connecting" ? "Подключение…" : "Отключено"}
        <span className="terminal-hint">Ctrl+C с выделением — копировать · Ctrl+V — вставить · Shift+мышь — выделить</span>
        <span className="spacer" />
        {state === "closed" && (
          <button type="button" onClick={() => setAttempt((n) => n + 1)}>
            Переподключиться
          </button>
        )}
      </div>
      {link && (
        <div className="terminal-link" role="region" aria-label="Ссылка из терминала">
          <span className="terminal-link-url" title={link}>
            {link}
          </span>
          <a className="button primary" href={link} target="_blank" rel="noopener noreferrer">
            Открыть
          </a>
          <button type="button" onClick={copyLink}>
            {copied ?? "Копировать"}
          </button>
          <form className="terminal-code" onSubmit={pasteCode}>
            <input
              aria-label="Вставить в терминал"
              placeholder="Код из браузера → сюда"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoComplete="off"
            />
            <button type="submit">Ввести</button>
          </form>
        </div>
      )}
      <div className="terminal-host" ref={host} />
    </div>
  );
}
