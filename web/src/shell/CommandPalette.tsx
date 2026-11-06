import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Bot, Code2, CornerDownLeft, FolderGit2, FolderPlus, LogOut, Moon, PanelLeft, Search, ShieldCheck, Sparkles, Sun } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api, errorText, type Me } from "../api";
import { queryClient, useProjects, useRuns } from "../data/queries";
import { Kbd, RUN_STATES, toast } from "../ui";
import { href, visibleNav } from "./routes";
import { useUi } from "./store";

interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: ReactNode;
  keywords?: string;
  run: () => void;
}

const go = (target: string) => () => {
  window.location.hash = target;
};

export function CommandPalette({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const { paletteOpen, setPalette, projectId, setProject, toggleSidebar, setCopilot, theme, setTheme } = useUi();
  const projects = useProjects();
  const runs = useRuns();
  const [text, setText] = useState("");
  const [index, setIndex] = useState(0);
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPalette(!useUi.getState().paletteOpen);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPalette]);

  useEffect(() => {
    if (paletteOpen) {
      setText("");
      setIndex(0);
    }
  }, [paletteOpen]);

  const commands = useMemo<Command[]>(() => {
    const current = projects.data?.find((p) => p.id === projectId);
    const out: Command[] = visibleNav(me).map((item) => ({
      id: `nav-${item.id}`,
      group: "Перейти",
      label: item.label,
      icon: <item.icon size={15} />,
      run: go(item.href),
    }));
    out.push(
      { id: "new-project", group: "Действия", label: "Подключить или создать проект", icon: <FolderPlus size={15} />, keywords: "new project git папка", run: go(href("new")) },
      {
        id: "new-task",
        group: "Действия",
        label: current ? `Новая AI-задача в «${current.name}»` : "Новая AI-задача",
        icon: <Sparkles size={15} />,
        keywords: "chat claude codex чат",
        run: go(current ? href("ai", current.id) : href("workspace")),
      },
      {
        id: "full-audit",
        group: "Действия",
        label: current ? `Полный аудит «${current.name}»` : "Полный аудит (выберите проект)",
        icon: <ShieldCheck size={15} />,
        keywords: "audit security scan",
        run: () => {
          if (!current) {
            window.location.hash = href("projects");
            return;
          }
          api.createRun(current.id, "audit", {}).then(
            (run) => {
              void queryClient.invalidateQueries({ queryKey: ["runs"] });
              toast({ tone: "success", title: "Аудит запущен", text: current.name });
              window.location.hash = href("run", run.id);
            },
            (err: unknown) => toast({ tone: "danger", title: "Аудит не запущен", text: errorText(err) }),
          );
        },
      },
      { id: "copilot", group: "Действия", label: "Открыть AI Copilot", icon: <Bot size={15} />, run: () => setCopilot(true) },
      { id: "sidebar", group: "Действия", label: "Свернуть или развернуть меню", icon: <PanelLeft size={15} />, run: toggleSidebar },
      {
        id: "theme",
        group: "Действия",
        label: theme === "arctic" ? "Тёмная тема" : "Светлая тема",
        icon: theme === "arctic" ? <Moon size={15} /> : <Sun size={15} />,
        keywords: "theme тема dark light",
        run: () => setTheme(theme === "arctic" ? "obsidian" : "arctic"),
      },
      { id: "logout", group: "Действия", label: "Выйти", icon: <LogOut size={15} />, run: onLogout },
    );
    for (const p of projects.data ?? []) {
      out.push(
        { id: `p-${p.id}`, group: "Проекты", label: p.name, hint: "обзор", icon: <FolderGit2 size={15} />, run: () => { setProject(p.id); go(href("project", p.id))(); } },
        { id: `pa-${p.id}`, group: "Проекты", label: `${p.name} — AI Workspace`, icon: <Sparkles size={15} />, run: () => { setProject(p.id); go(href("ai", p.id))(); } },
        { id: `pc-${p.id}`, group: "Проекты", label: `${p.name} — Code Studio`, icon: <Code2 size={15} />, run: () => { setProject(p.id); go(href("code", p.id))(); } },
      );
    }
    for (const r of (runs.data ?? []).slice(0, 8)) {
      out.push({
        id: `r-${r.id}`,
        group: "Последние задачи",
        label: r.goal || r.kind || r.id,
        hint: `${r.project_name} · ${RUN_STATES[r.state]?.label ?? r.state}`,
        icon: <Search size={15} />,
        keywords: r.id,
        run: go(href("run", r.id)),
      });
    }
    return out;
  }, [me, projects.data, runs.data, projectId, theme, onLogout, setCopilot, setProject, setTheme, toggleSidebar]);

  const shown = useMemo(() => {
    const words = text.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return commands.filter((c) => c.group !== "Проекты" || !c.id.startsWith("pc-"));
    return commands.filter((c) => {
      const hay = `${c.label} ${c.hint ?? ""} ${c.keywords ?? ""} ${c.group}`.toLowerCase();
      return words.every((w) => hay.includes(w));
    });
  }, [commands, text]);

  useEffect(() => setIndex(0), [text]);
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-index="${index}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [index]);

  function choose(command: Command | undefined) {
    if (!command) return;
    setPalette(false);
    command.run();
  }

  function onKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setIndex((i) => Math.min(shown.length - 1, i + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setIndex((i) => Math.max(0, i - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(shown[index]);
    }
  }

  let lastGroup = "";
  return (
    <DialogPrimitive.Root open={paletteOpen} onOpenChange={setPalette}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="overlay" />
        <DialogPrimitive.Content className="palette" aria-label="Командная палитра">
          <DialogPrimitive.Title className="sr-only">Командная палитра</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">Найдите раздел, проект, задачу или действие</DialogPrimitive.Description>
          <div className="palette-input">
            <Search size={16} aria-hidden />
            <input
              autoFocus
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onKey}
              placeholder="Раздел, проект, задача или действие…"
              aria-label="Команда"
              role="combobox"
              aria-expanded="true"
              aria-controls="palette-list"
              aria-activedescendant={shown[index] ? `cmd-${shown[index]?.id}` : undefined}
            />
            <Kbd>Esc</Kbd>
          </div>
          <div className="palette-list" id="palette-list" role="listbox" ref={list} aria-label="Команды">
            {shown.length === 0 && <div className="palette-empty">Ничего не найдено</div>}
            {shown.map((c, i) => {
              const head = c.group !== lastGroup ? c.group : null;
              lastGroup = c.group;
              return (
                <div key={c.id}>
                  {head && <div className="palette-group">{head}</div>}
                  <div
                    id={`cmd-${c.id}`}
                    role="option"
                    aria-selected={i === index}
                    data-index={i}
                    className={i === index ? "palette-item active" : "palette-item"}
                    onMouseMove={() => setIndex(i)}
                    onClick={() => choose(c)}
                  >
                    <span className="palette-icon">{c.icon}</span>
                    <span className="truncate">{c.label}</span>
                    {c.hint && <span className="palette-hint truncate">{c.hint}</span>}
                    {i === index && <CornerDownLeft size={13} className="palette-enter" aria-hidden />}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="palette-foot">
            <span>
              <Kbd>↑</Kbd> <Kbd>↓</Kbd> выбрать
            </span>
            <span>
              <Kbd>Enter</Kbd> открыть
            </span>
            <span>
              <Kbd>Ctrl</Kbd> <Kbd>K</Kbd> закрыть
            </span>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
