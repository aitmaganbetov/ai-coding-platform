// End-to-end smoke test of the Web UI against a running server (manual / local).
// Usage: node web/e2e/smoke.mjs http://127.0.0.1:8000 USERNAME PASSWORD [SCREENSHOT_DIR]
// Expects a project the user can run in. Fails on any console error (e.g. a CSP violation).
import { createRequire } from "node:module";

// Playwright is resolved like require() (a global or project install both work).
const { chromium } = createRequire(import.meta.url)("playwright");

const [base, username, password, shots] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
const problems = [];
page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
page.on("pageerror", (e) => problems.push(String(e)));

await page.goto(base);
await page.getByLabel("Пользователь").fill(username);
await page.getByLabel("Пароль").fill(password);
await page.getByRole("button", { name: "Войти" }).click();
await page.getByRole("heading", { name: "Проекты" }).waitFor();
if (shots) await page.screenshot({ path: `${shots}/1-projects.png` });
await page.locator("table a").first().click();
await page.getByRole("heading", { name: "Прогоны" }).waitFor();
await page.locator("select[name=kind]").selectOption("discover");
await page.getByRole("button", { name: "Запустить" }).click();
await page.getByText("COMPLETED").first().waitFor({ timeout: 60_000 });
if (shots) await page.screenshot({ path: `${shots}/2-run.png`, fullPage: true });
const artifacts = await page.locator("button.link").count();
await page.getByRole("link", { name: "← к проекту" }).click();
await page.getByRole("heading", { name: "Журнал аудита" }).waitFor();
if (shots) await page.screenshot({ path: `${shots}/3-project.png`, fullPage: true });
await browser.close();
if (problems.length) { console.error("console errors:", problems); process.exit(1); }
if (artifacts < 1) { console.error("no artifacts listed"); process.exit(1); }
console.log(`ok: run completed, ${artifacts} artifact(s), no console errors`);
