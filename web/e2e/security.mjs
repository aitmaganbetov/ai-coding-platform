// End-to-end check of the AI Security Center against a running server (manual / staging).
// Usage: node web/e2e/security.mjs http://127.0.0.1:8000 USERNAME PASSWORD on|off [SCREENSHOT_DIR]
//
// on:  the server runs with AI_CODING_SERVER_SECURITY_CENTER=on and the user can view the
//      findings of a project with at least one run. For every width (320 … 1920 px) the page
//      renders without horizontal overflow or console error (a CSP violation included); the
//      coverage cards show their states, DAST and containers are "not implemented"; a
//      finding opens with its safe details; nothing a scanner reported runs as HTML.
// off: the module is off (default, or after a rollback): no navigation entry, the API
//      answers 404 and the page says the module is disabled.
// Read-only: it starts no run and changes nothing.
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");
const [base, username, password, expect, shots] = process.argv.slice(2);
if (!["on", "off"].includes(expect)) {
  console.error("usage: security.mjs BASE USERNAME PASSWORD on|off [SCREENSHOT_DIR]");
  process.exit(2);
}
const WIDTHS = [320, 375, 768, 1024, 1440, 1920];
const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const problems = [];
const fail = (message) => problems.push(message);
page.on("console", (m) => {
  if (m.type() === "error") fail(`${page.url()}: ${m.text()}`);
});
page.on("pageerror", (e) => fail(`${page.url()}: ${e}`));
page.on("dialog", (d) => {
  fail(`a scanner string opened a ${d.type()} dialog: ${d.message()}`);
  void d.dismiss();
});

await page.goto(base);
await page.getByLabel("Пользователь").fill(username);
await page.getByLabel("Пароль").fill(password);
await page.getByRole("button", { name: "Войти" }).click();
const nav = page.getByRole("complementary", { name: "Навигация" });
await nav.waitFor();

const inNav = await nav.getByRole("link", { name: "Security Center" }).count();
// Probes go through the test's own HTTP client: an expected 404 is not a page error.
const headers = { Authorization: `Bearer ${await page.evaluate(() => sessionStorage.getItem("ai-coding-token"))}` };
const me = await (await page.request.get(`${base}/api/v1/me`, { headers })).json();
const projects = await (await page.request.get(`${base}/api/v1/projects`, { headers })).json();
const nil = "00000000-0000-0000-0000-000000000000";
const probe = await page.request.get(`${base}/api/v1/projects/${projects[0]?.id ?? nil}/security-center/runs/${nil}`, { headers });
const api = { feature: me.features?.security_center === true, probe: probe.status() };

if (expect === "off") {
  if (inNav) fail("navigation shows Security Center while the module is off");
  if (api.feature) fail("GET /me reports the module on");
  if (api.probe !== 404) fail(`the Security Center API answered ${api.probe}, not 404`);
  await page.goto(`${base}/#/security`);
  await page.getByText("Модуль выключен на этом сервере").waitFor();
} else {
  if (!inNav) fail("navigation has no Security Center entry");
  if (!api.feature) fail("GET /me reports the module off");
  if (api.probe !== 404) fail(`an unknown run answered ${api.probe}, not 404`);
  const overflow = [];
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: width < 768 ? 800 : 900 });
    await page.goto(`${base}/#/security`);
    await page.getByRole("region", { name: "Покрытие сканирования" }).waitFor({ timeout: 15_000 });
    await page.waitForTimeout(500);
    const wide = await page.evaluate(() => {
      const main = document.querySelector("main");
      return { page: document.documentElement.scrollWidth - window.innerWidth, main: main ? main.scrollWidth - main.clientWidth : 0 };
    });
    if (wide.page > 1 || wide.main > 1) overflow.push(`@${width}: page +${wide.page}px, main +${wide.main}px`);
    if (shots && [375, 1440].includes(width)) await page.screenshot({ path: `${shots}/security-${width}.png`, fullPage: true });
  }
  if (overflow.length) fail(`horizontal overflow: ${overflow.join(", ")}`);

  await page.setViewportSize({ width: 1440, height: 900 });
  const coverage = page.getByRole("region", { name: "Покрытие сканирования" });
  for (const card of ["SAST", "SCA — зависимости", "Секреты", "IaC", "Контейнеры", "DAST"]) {
    if ((await coverage.getByText(card, { exact: true }).count()) !== 1) fail(`coverage card ${card} missing`);
  }
  if ((await coverage.getByText("Не реализовано").count()) < 2) fail("DAST and containers are not marked as not implemented");
  if ((await page.getByText("не оценивалось").count()) !== 1) fail("compliance is not shown as not assessed");

  const rows = page.getByRole("region", { name: "Находки run'а" }).locator("tbody tr");
  if ((await rows.count()) > 0) {
    await rows.first().click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor();
    await dialog.getByText(/не выдаются/).waitFor();
    if (shots) await page.screenshot({ path: `${shots}/security-finding.png` });
    await page.keyboard.press("Escape");
  }
  const injected = await page.evaluate(() => document.querySelectorAll("main [onerror], main script, main iframe").length);
  if (injected) fail(`${injected} element(s) from scanner data were rendered as HTML`);
  if (await page.evaluate(() => "pwned" in window)) fail("a scanner string ran as script");
}

await browser.close();
if (problems.length) {
  console.error("security center check failed:\n  " + problems.join("\n  "));
  process.exit(1);
}
console.log(expect === "on" ? `ok: Security Center on, ${WIDTHS.length} widths, details, no HTML from scanners` : "ok: Security Center off (no navigation, API 404)");
