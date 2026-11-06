// End-to-end check of the AI Changes Review in a real workspace container (manual / local).
// Usage: node web/e2e/review.mjs http://127.0.0.1:8000 USERNAME PASSWORD PROJECT_ID [SCREENSHOT_DIR]
// Needs a server with workspaces (AI_CODING_SERVER_WORKSPACES=local and the workspace image).
// Changes a file in two places and adds one through the API, then in the browser: rejects
// one hunk, accepts the other and the new file, switches to side by side, commits what was
// accepted — and checks the files and git status. No console error, no page overflow.
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

/** The REST API with the page's own session. */
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
const run = Date.now().toString(36); // fresh files on every run
const target = `app/review-${run}.py`;
const fresh = `docs/review-${run}.md`;
await call("POST", `${p}/workspace`, {});
const lines = Array.from({ length: 20 }, (_, i) => `line ${i + 1}\n`);
await call("PUT", `${p}/file?path=${target}`, { content: lines.join(""), create: true });
let base1 = null;
for (let attempt = 0; attempt < 5 && !base1?.data?.ok; attempt++) {
  if (attempt) await page.waitForTimeout(3000); // the container may still be starting
  base1 = await call("POST", `${p}/workspace/commit`, { message: `review base ${run}` });
}
if (!base1.data.ok) throw new Error(`base commit failed: ${JSON.stringify(base1)}`);
const { data: file } = await call("GET", `${p}/file?path=${target}`);
lines[1] = "LINE 2 accepted\n";
lines[17] = "LINE 18 rejected\n";
await call("PUT", `${p}/file?path=${target}`, { content: lines.join(""), version: file.version, create: false });
await call("PUT", `${p}/file?path=${fresh}`, { content: "# new\n", create: true });

await page.goto(`${base}/#/projects/${projectId}/ai?tab=diff`);
await page.getByText("Проверка изменений").waitFor({ timeout: 90_000 }); // the container starts
await page.getByRole("article", { name: `${target}: изменён` }).waitFor();
if (shots) await page.screenshot({ path: `${shots}/review-unified.png`, fullPage: true });

await page.getByRole("button", { name: "Отклонить фрагмент 2" }).click();
await page.getByRole("button", { name: "Отклонить фрагмент 2" }).waitFor({ state: "detached" });
await page.getByRole("button", { name: "Принять фрагмент 1" }).click();
await page.getByRole("button", { name: "Вернуть фрагмент 1 на проверку" }).waitFor();
await page.getByRole("article", { name: `${fresh}: новый` }).getByRole("button", { name: "Принять файл" }).click();
await page.getByRole("article", { name: `${fresh}: новый` }).or(page.getByText(/На проверке: 0/)).first().waitFor();
await page.getByRole("radio", { name: "Рядом" }).click();
if (shots) await page.screenshot({ path: `${shots}/review-split.png`, fullPage: true });

await page.setViewportSize({ width: 375, height: 800 });
await page.waitForTimeout(400);
const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
if (wide > 1) problems.push(`page overflows by ${wide}px at 375px`);
if (shots) await page.screenshot({ path: `${shots}/review-375.png`, fullPage: true });
await page.setViewportSize({ width: 1440, height: 900 });

await page.getByLabel("Сообщение коммита принятых изменений").fill("reviewed in NEXUS");
await page.getByRole("button", { name: "Зафиксировать принятое" }).click();
await page.getByText("Принятые изменения зафиксированы").waitFor();

const { data: after } = await call("GET", `${p}/file?path=${target}`);
if (!after.content.includes("LINE 2 accepted") || !after.content.includes("line 18\n") || after.content.includes("LINE 18 rejected"))
  problems.push(`unexpected file content:\n${after.content}`);
const { data: status } = await call("GET", `${p}/workspace/git`);
if (status.files.length) problems.push(`workspace not clean: ${JSON.stringify(status.files)}`);
await call("POST", `${p}/workspace/restart`, {}); // stop the test's container

await browser.close();
if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log("ok: hunk rejected, hunk and new file accepted, side by side, commit of the accepted, clean tree");
