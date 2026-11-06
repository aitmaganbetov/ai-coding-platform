// End-to-end check of AI in the editor, in a real workspace container (manual / local).
// Usage: node web/e2e/editor.mjs http://127.0.0.1:8000 USERNAME PASSWORD PROJECT_ID [SCREENSHOT_DIR]
// The "agent" changes a file in two places (through the API); in Code Studio the editor
// marks both changes; one is rejected in place (the file reverts), the other accepted; a
// selection's «Объяснить» reaches the workspace's chat with the file and the lines.
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");
const [base, username, password, projectId, shots] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const problems = [];
page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
page.on("pageerror", (e) => problems.push(String(e)));

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
const name = `editor-${run}.py`;
const target = `app/${name}`;
await call("POST", `${p}/workspace`, {});
const lines = Array.from({ length: 20 }, (_, i) => `line_${i + 1} = ${i + 1}\n`);
await call("PUT", `${p}/file?path=${target}`, { content: lines.join(""), create: true });
let committed = null;
for (let i = 0; i < 5 && !committed?.data?.ok; i++) {
  if (i) await page.waitForTimeout(3000);
  committed = await call("POST", `${p}/workspace/commit`, { message: `editor base ${run}` });
}
if (!committed.data.ok) throw new Error(`base commit failed: ${JSON.stringify(committed)}`);
const { data: file } = await call("GET", `${p}/file?path=${target}`);
lines[1] = "line_2 = 'agent kept'\n";
lines[17] = "line_18 = 'agent dropped'\n";
await call("PUT", `${p}/file?path=${target}`, { content: lines.join(""), version: file.version, create: false });

await page.goto(`${base}/#/projects/${projectId}/code`);
await page.getByRole("button", { name: /^▸ app$/ }).click();
await page.getByRole("button", { name, exact: true }).click();
await page.getByRole("group", { name: "Изменение 1" }).waitFor({ timeout: 60_000 });
await page.getByRole("group", { name: "Изменение 2" }).waitFor();
if (shots) await page.screenshot({ path: `${shots}/editor-marks.png` });

await page.getByRole("button", { name: "Отклонить изменение 2" }).click();
await page.getByRole("group", { name: "Изменение 2" }).waitFor({ state: "detached" });
await page.getByRole("button", { name: "Принять изменение 1" }).click();
await page.getByRole("group", { name: "Изменение 1" }).waitFor({ state: "detached" });
const { data: after } = await call("GET", `${p}/file?path=${target}`);
if (!after.content.includes("agent kept") || after.content.includes("agent dropped")) problems.push(`file content:\n${after.content}`);
const { data: diff } = await call("GET", `${p}/workspace/diff`);
if (!diff.accepted.some((f) => f.path === target)) problems.push("the accepted change is not in the index");

// A selection → «Объяснить» → the chat beside the editor gets the request.
await page.locator(".cm-content").click();
await page.keyboard.press("Control+Home");
for (let i = 0; i < 4; i++) await page.keyboard.press("ArrowDown");
await page.keyboard.press("Shift+ArrowDown");
await page.keyboard.press("Shift+ArrowDown");
await page.getByRole("toolbar", { name: "ИИ для выделенного кода" }).waitFor();
if (shots) await page.screenshot({ path: `${shots}/editor-actions.png` });
await page.getByRole("button", { name: "ИИ: Объяснить" }).click();
const asked = page.getByText(`Объясни, что делает этот код в \`${target}\` (строки 5–6)`, { exact: false }).first();
await asked.waitFor({ timeout: 90_000 }).catch(async (err) => {
  if (shots) await page.screenshot({ path: `${shots}/editor-chat-failed.png` });
  throw err;
});
if (shots) await page.screenshot({ path: `${shots}/editor-chat.png` });

// Leave the workspace page first: it would still poll the container being stopped.
await page.goto(`${base}/#/dashboard`);
await call("POST", `${p}/workspace/restart`, {}); // stop the test's container
await browser.close();
// The chat's own messages (no Claude sign-in on a test server) are not page errors.
const real = problems.filter((m) => !/401|WebSocket/.test(m));
if (real.length) {
  console.error(real.join("\n"));
  process.exit(1);
}
console.log("ok: changes marked in the editor, one rejected in place, one accepted, a selection asked the chat");
