const { test, expect } = require("@playwright/test");
test("greets the user", async ({ page }) => {
  await page.setContent("<h1>Hello</h1>");
  await expect(page.locator("h1")).toHaveText("Hello");
});
test("shows the total", async ({ page }) => {
  await page.setContent("<p id=total>41</p>");
  await expect(page.locator("#total")).toHaveText("42", { timeout: 1000 });
});
