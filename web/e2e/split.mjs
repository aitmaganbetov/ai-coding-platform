// End-to-end check of editor tabs, Split View and workspace tabs (ADR-0045), in a real
// workspace container (manual / local).
// Usage: node web/e2e/split.mjs http://127.0.0.1:8000 USERNAME PASSWORD PROJECT_ID [SCREENSHOT_DIR]
// Two files open as tabs; an unsaved edit is marked and moves with its tab into the right
// group, where it is saved; the divider resizes the groups; a link opens a file at a line;
// full screen comes and goes with Esc; the workspace tabs switch and close; the tabs are back
// after a reload; at 320px nothing overflows and one group is shown.
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");
const [base, username, password, projectId, shots] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const problems = [];
page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
page.on("pageerror", (e) => problems.push(String(e)));
const check = (ok, message) => ok || problems.push(message);

await page.goto(base);
await page.getByLabel("Пользователь").fill(username);
await page.getByLabel("Пароль").fill(password);
await page.getByRole("button", { name: "Войти" }).click();
await page.getByRole("complementary", { name: "Навигация" }).waitFor();

const call = (method, path, body) =>
  page.evaluate(
    async ([method, path, body]) => {
      const token = sessionStorage.getItem("ai-coding-token");
      const res = await fetch(`/api/v1${path}`, {
        method,
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      return { status: res.status, data: res.status === 204 ? null : await res.json() };
    },
    [method, path, body],
  );

const p = `/projects/${projectId}`;
const run = Date.now().toString(36);
const dir = `split${run}`;
const a = `${dir}/alpha.py`;
const b = `${dir}/beta.py`;
await call("POST", `${p}/workspace`, {});
let put = null;
for (let i = 0; i < 20 && put?.status !== 200 && put?.status !== 201; i++) {
  if (i) await page.waitForTimeout(3000); // the container is starting
  put = await call("PUT", `${p}/file?path=${a}`, { content: Array.from({ length: 40 }, (_, i) => `line_${i + 1} = ${i + 1}\n`).join(""), create: true });
}
if (put.status >= 300) throw new Error(`cannot write ${a}: ${JSON.stringify(put)}`);
await call("PUT", `${p}/file?path=${b}`, { content: "def beta():\n    return 1\n", create: true });

// Two files → two tabs in one group.
await page.goto(`${base}/#/projects/${projectId}/code`);
await page.getByRole("button", { name: `▸ ${dir}` }).click({ timeout: 60_000 });
await page.getByRole("button", { name: "alpha.py", exact: true }).click();
await page.locator(".ed-pane:not([hidden]) .cm-content").waitFor();
await page.getByRole("button", { name: "beta.py", exact: true }).click();
const tab = (name) => page.getByRole("tab", { name: new RegExp(`^${name.replace(".", "\\.")}`) });
await tab("beta.py").waitFor();
check((await tab("alpha.py").getAttribute("aria-selected")) === "false", "alpha.py should be a background tab");
check((await page.getByRole("tablist", { name: /^Вкладки:/ }).count()) === 1, "one editor group expected");

// An unsaved edit: the dot, then «Разделить редактор» takes it to the right group.
await page.locator(".ed-pane:not([hidden]) .cm-content").click();
await page.keyboard.press("Control+End");
await page.keyboard.type("# edited in split\n");
await tab("beta.py").getByRole("img", { name: "есть несохранённые изменения" }).waitFor();
await page.getByRole("button", { name: "Разделить редактор" }).click();
const right = page.getByRole("region", { name: "Правая группа редактора" });
const left = page.getByRole("region", { name: "Левая группа редактора" });
await right.getByRole("tab", { name: /^beta\.py/ }).waitFor();
check((await left.getByRole("tab", { name: /^alpha\.py/ }).count()) === 1, "alpha.py should stay on the left");
check((await right.locator(".ed-pane:not([hidden]) .cm-content").textContent()).includes("# edited in split"), "the unsaved text should move with the tab");
if (shots) await page.screenshot({ path: `${shots}/split-two-groups.png` });

// Saved from the right group.
await right.getByRole("button", { name: "Сохранить" }).click();
await right.getByRole("img", { name: "есть несохранённые изменения" }).waitFor({ state: "detached" });
const { data: saved } = await call("GET", `${p}/file?path=${b}`);
check(saved.content.includes("# edited in split"), `beta.py on disk:\n${saved.content}`);

// The divider resizes the groups.
const divider = page.getByRole("separator", { name: "Граница групп редактора" });
const before = Number(await divider.getAttribute("aria-valuenow"));
const box = await divider.boundingBox();
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await page.mouse.down();
await page.mouse.move(box.x - 200, box.y + box.height / 2, { steps: 5 });
await page.mouse.up();
const after = Number(await divider.getAttribute("aria-valuenow"));
check(after < before - 5, `divider: ${before}% → ${after}%`);

// A link opens a file at a line: shown, highlighted, centred.
await page.evaluate((target) => (window.location.hash = target), `#/projects/${projectId}/code?file=${encodeURIComponent(a)}&line=33`);
const target = page.locator(".cm-target-line");
await target.waitFor();
check((await target.textContent()).trim() === "line_33 = 33", `target line: ${await target.textContent()}`);
check((await left.getByRole("tab", { name: /^alpha\.py/ }).getAttribute("aria-selected")) === "true", "alpha.py should be shown");
if (shots) await page.screenshot({ path: `${shots}/split-line.png` });
const scrolled = await page.evaluate(() => { const m = document.getElementById("main"); return m.scrollHeight - m.clientHeight; });
check(scrolled <= 1, `the main area scrolls by ${scrolled}px: the workspace tabs slide away`);

// Full screen: no sidebar, no workspace tabs; Esc leaves it.
const sidebar = page.getByRole("complementary", { name: "Навигация" });
const strip = page.getByRole("navigation", { name: "Открытые рабочие области" });
await strip.waitFor();
await page.getByRole("button", { name: "Во весь экран" }).click();
await sidebar.waitFor({ state: "hidden" });
check(!(await strip.isVisible()), "the workspace tabs should hide in full screen");
if (shots) await page.screenshot({ path: `${shots}/split-fullscreen.png` });
await page.keyboard.press("Escape");
await sidebar.waitFor();

// Workspace tabs: the AI Workspace joins the strip; switch back, close it.
await page.evaluate((target) => (window.location.hash = target), `#/projects/${projectId}/ai`);
await strip.getByRole("link", { name: /AI Workspace/ }).waitFor();
check((await strip.getByRole("link", { name: /AI Workspace/ }).getAttribute("aria-current")) === "page", "the AI Workspace tab should be current");
await strip.getByRole("link", { name: /Code Studio/ }).click();
await page.waitForURL(/\/code$/);
await strip.getByRole("button", { name: /^Закрыть вкладку .* — AI Workspace$/ }).click();
check((await strip.getByRole("link").count()) === 1, "one workspace tab should remain");

// The tabs come back after a reload (paths only in localStorage).
await page.reload();
await right.getByRole("tab", { name: /^beta\.py/ }).waitFor({ timeout: 30_000 });
await left.getByRole("tab", { name: /^alpha\.py/ }).waitFor();
const stored = await page.evaluate((id) => localStorage.getItem(`nexus-tabs-${id}`), projectId);
check(!stored.includes("line_") && !stored.includes("edited"), `localStorage keeps contents: ${stored}`);

// Phones: one group at a time, nothing wider than the screen.
await page.setViewportSize({ width: 320, height: 700 });
await page.getByRole("radiogroup", { name: "Группа редактора" }).or(page.locator(".ed-switch")).first().waitFor();
check((await page.locator(".ed-group:visible").count()) === 1, "one editor group at 320px");
const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
check(overflow <= 0, `the page overflows by ${overflow}px at 320px`);
if (shots) await page.screenshot({ path: `${shots}/split-320.png` });

// Leave the workspace page first: it would still poll the container being stopped.
await page.goto(`${base}/#/dashboard`);
await page.waitForTimeout(2000); // a hash change: the last polls of the workspace are still in flight
const checked = problems.length; // stopping the container makes late polls fail: not the UI's errors
await call("POST", `${p}/workspace/restart`, {}); // stop the test's container
await browser.close();
// The chat's own messages (no Claude sign-in on a test server) are not page errors.
const real = problems.slice(0, checked).filter((m) => !/401|WebSocket/.test(m));
if (real.length) {
  console.error(real.join("\n"));
  process.exit(1);
}
console.log("ok: tabs, an unsaved edit moved and saved in the right group, divider, open at a line, full screen, workspace tabs, restore, 320px");
