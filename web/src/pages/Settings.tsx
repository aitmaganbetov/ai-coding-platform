import { Keyboard, Monitor, Moon, Palette, Settings as SettingsIcon, Sun, User, Users as UsersIcon } from "lucide-react";
import { Account } from "../Account";
import type { Me } from "../api";
import { href } from "../shell/routes";
import { ACCENTS, type ThemeChoice, useUi } from "../shell/store";
import { Card, cx, Kbd, PageHeader, Tabs } from "../ui";
import { Users } from "../Users";

const THEMES: { value: ThemeChoice; label: string; text: string; icon: typeof Sun }[] = [
  { value: "system", label: "Как в системе", text: "Obsidian или Arctic по настройке ОС", icon: Monitor },
  { value: "obsidian", label: "Obsidian Dark", text: "Основная тёмная тема", icon: Moon },
  { value: "arctic", label: "Arctic Light", text: "Светлая тема для дневной работы", icon: Sun },
  { value: "oled", label: "Midnight OLED", text: "Почти чёрная, для OLED-экранов", icon: Moon },
];

const SHORTCUTS: [string[], string][] = [
  [["Ctrl", "K"], "Командная палитра: разделы, проекты, задачи, действия"],
  [["↑", "↓", "Enter"], "Выбор и запуск команды в палитре"],
  [["Esc"], "Закрыть палитру, диалог или меню"],
  [["Ctrl", "S"], "Сохранить файл в Code Studio"],
  [["Enter"], "Отправить сообщение в чат (Shift+Enter — новая строка)"],
  [["←", "→"], "Переключение вкладок"],
];

function Appearance() {
  const { theme, setTheme, accent, setAccent } = useUi();
  return (
    <div className="stack">
      <Card title="Тема" icon={<Palette size={15} />}>
        <div className="theme-grid" role="radiogroup" aria-label="Тема оформления">
          {THEMES.map(({ value, label, text, icon: Icon }) => (
            <button key={value} type="button" role="radio" aria-checked={theme === value} className={cx("theme-card", theme === value && "active")} onClick={() => setTheme(value)}>
              <span className={`theme-preview preview-${value}`} aria-hidden>
                <span />
                <span />
                <span />
              </span>
              <span className="theme-name">
                <Icon size={14} aria-hidden /> {label}
              </span>
              <small>{text}</small>
            </button>
          ))}
        </div>
      </Card>
      <Card title="Акцентный цвет" icon={<Palette size={15} />}>
        <div className="accent-row" role="group" aria-label="Акцентный цвет">
          {ACCENTS.map((c) => (
            <button key={c} type="button" className={cx("swatch swatch-lg", (accent ?? ACCENTS[0]) === c && "on")} style={{ background: c }} aria-label={`Акцент ${c}`} aria-pressed={(accent ?? ACCENTS[0]) === c} onClick={() => setAccent(c === ACCENTS[0] ? null : c)} />
          ))}
          <label className="swatch swatch-lg custom" title="Свой цвет">
            <input type="color" aria-label="Свой акцентный цвет" value={accent ?? ACCENTS[0]} onChange={(e) => setAccent(e.target.value)} />
          </label>
        </div>
        <p className="text-muted">Настройки внешнего вида хранятся в этом браузере. Анимации отключаются сами, если в системе включено «уменьшить движение».</p>
      </Card>
    </div>
  );
}

function Shortcuts() {
  return (
    <Card title="Горячие клавиши" icon={<Keyboard size={15} />}>
      <dl className="kv">
        {SHORTCUTS.map(([keys, text]) => (
          <div key={text} style={{ display: "contents" }}>
            <dt>
              {keys.map((k) => (
                <Kbd key={k}>{k}</Kbd>
              ))}
            </dt>
            <dd>{text}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

export default function Settings({ me, tab = "appearance", onLogout }: { me: Me; tab?: string; onLogout: () => void }) {
  const tabs = [
    { id: "appearance", label: "Внешний вид", icon: <Palette size={14} /> },
    { id: "account", label: "Профиль и токены", icon: <User size={14} /> },
    ...(me.is_admin ? [{ id: "users", label: "Пользователи", icon: <UsersIcon size={14} /> }] : []),
    { id: "shortcuts", label: "Горячие клавиши", icon: <Keyboard size={14} /> },
  ];
  const current = tabs.some((t) => t.id === tab) ? tab : "appearance";
  return (
    <div className="page">
      <PageHeader icon={<SettingsIcon size={18} />} title="Настройки" subtitle="Внешний вид, профиль, токены API и пользователи сервера." />
      <Tabs label="Разделы настроек" items={tabs} value={current} onChange={(id) => (window.location.hash = href("settings", id))} />
      {current === "appearance" && <Appearance />}
      {current === "account" && <Account me={me} onLogout={onLogout} />}
      {current === "users" && me.is_admin && <Users me={me} />}
      {current === "shortcuts" && <Shortcuts />}
    </div>
  );
}
