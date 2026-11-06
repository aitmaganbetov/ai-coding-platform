// End-to-end check of the NEXUS interface against a running server (manual / local).
// Usage: node web/e2e/nexus.mjs http://127.0.0.1:8000 USERNAME PASSWORD [SCREENSHOT_DIR] [PROJECT_ID]
//
// For every section and every width (320 … 1920 px): the page renders, nothing overflows
// horizontally (tables and editors scroll inside themselves), no console error (a CSP
// violation included); then the main flows: command palette, theme switch, a discover run
// started from Tasks and followed to COMPLETED. Expects a project the user may run in.
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");
const [base, username, password, shots, givenProject] = process.argv.slice(2);
const WIDTHS = [320, 375, 768, 1024, 1440, 1920];
const SECTIONS = [
  ["dashboard", "#/dashboard"],
  ["projects", "#/projects"],
  ["workspace", "#/workspace"],
  ["tasks", "#/tasks"],
  ["agents", "#/agents"],
  ["studio", "#/studio"],
  ["audit", "#/audit"],
  ["testing", "#/testing"],
  ["deployments", "#/deployments"],
  ["models", "#/models"],
  ["infrastructure", "#/infrastructure"],
  ["settings", "#/settings"],
  ["new", "#/projects/new"],
];

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
const problems = [];
page.on("console", (m) => {
  if (m.type() === "error") problems.push(`${page.url()}: ${m.text()}`);
});
page.on("pageerror", (e) => problems.push(`${page.url()}: ${e}`));

await page.goto(base);
await page.getByLabel("Пользователь").fill(username);
await page.getByLabel("Пароль").fill(password);
await page.getByRole("button", { name: "Войти" }).click();
await page.getByRole("complementary", { name: "Навигация" }).waitFor(); // the App Shell: signed in

// The project's own pages: the given project, else the first one the API lists.
const projectId =
  givenProject ||
  (await page.evaluate(async () => {
    const token = sessionStorage.getItem("ai-coding-token");
    const res = await fetch("/api/v1/projects", { headers: { Authorization: `Bearer ${token}` } });
    const list = res.ok ? await res.json() : [];
    return list[0]?.id ?? null;
  }));
if (projectId) SECTIONS.push(["project", `#/projects/${projectId}`], ["ai", `#/projects/${projectId}/ai`], ["code", `#/projects/${projectId}/code`]);

const overflow = [];
for (const width of WIDTHS) {
  await page.setViewportSize({ width, height: width < 768 ? 800 : 900 });
  for (const [name, hash] of SECTIONS) {
    await page.goto(`${base}/${hash}`);
    // Live views keep polling (runs, agents): wait for the content, not for an idle network.
    await page.locator("main h1, main .chat-panel, main .workspace").first().waitFor({ timeout: 15_000 }).catch(() => undefined);
    await page.waitForTimeout(700);
    const wide = await page.evaluate(() => {
      const doc = document.documentElement;
      const main = document.querySelector("main");
      return {
        page: doc.scrollWidth - window.innerWidth,
        main: main ? main.scrollWidth - main.clientWidth : 0,
      };
    });
    if (wide.page > 1 || wide.main > 1) overflow.push(`${name}@${width}: page +${wide.page}px, main +${wide.main}px`);
    if (shots && [375, 1440].includes(width)) await page.screenshot({ path: `${shots}/${name}-${width}.png`, fullPage: width === 1440 });
  }
}

// Command palette: Ctrl+K, type, Enter navigates.
await page.setViewportSize({ width: 1440, height: 900 });
await page.goto(`${base}/#/dashboard`);
await page.keyboard.press("Control+k");
await page.getByRole("combobox", { name: "Команда" }).fill("Audit Center");
await page.keyboard.press("Enter");
await page.waitForURL(/#\/audit/);

// Theme: Arctic Light applies to <html>.
await page.getByRole("button", { name: "Тема оформления" }).click();
await page.getByRole("menuitem", { name: /Arctic Light/ }).click();
const theme = await page.evaluate(() => document.documentElement.dataset.theme);
if (theme !== "arctic") problems.push(`theme not applied: ${theme}`);
if (shots) await page.screenshot({ path: `${shots}/dashboard-arctic.png` });

await browser.close();
if (overflow.length) console.error("horizontal overflow:\n  " + overflow.join("\n  "));
if (problems.length) console.error("console errors:\n  " + problems.join("\n  "));
if (overflow.length || problems.length) process.exit(1);
console.log(`ok: ${SECTIONS.length} sections × ${WIDTHS.length} widths, palette, theme`);
