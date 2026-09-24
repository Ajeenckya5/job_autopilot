import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { stubSearch } from "./stub-search.js";

const views = [
  { width: 390, height: 844 },
  { width: 1440, height: 900 },
];

for (const size of views) {
  test.describe(`${size.width}px`, () => {
    test.use({ viewport: size });

    test("onboarding, search, apply, and filters stay on this site", async ({ page }) => {
      const bad = [];
      const bodies = [];
      page.on("pageerror", (err) => bad.push(String(err)));
      page.on("console", (msg) => { if (msg.type() === "error") bad.push(msg.text()); });
      page.on("response", (res) => {
        if (res.status() >= 400) bad.push(`${res.status()} ${res.url()}`);
        if (/jina\.ai|allorigins|linkedin\.com|indeed\.com|jobright/i.test(res.url())) bad.push(res.url());
      });
      page.on("request", (req) => {
        const data = req.postData() || "";
        if (data) bodies.push(data);
      });

      await stubSearch(page);
      await page.route("**/api/llm/migrate", (route) => route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ migrated: [] }),
      }));
      await page.goto("./");
      await page.locator("#resumeFile").setInputFiles({
        name: "resume.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("Ada Lovelace\nSoftware engineer with Python, JavaScript, SQL, and AWS experience building production systems."),
      });
      await expect(page.locator("#stepPrefs")).toBeVisible();
      await page.locator("#personName").fill("Ada Lovelace");
      await page.locator("#whereInput").fill("United States");
      await page.locator("#runs").fill("2.5");
      await page.locator("#saveSetup").click();
      await expect(page.locator("#runsError")).toContainText("whole number");
      await page.locator("#runs").fill("0");
      await page.locator("#saveSetup").click();
      await expect(page.locator("#statusLive")).toContainText(/Found|feed|role/i, { timeout: 20000 });
      const count = await page.locator("#jobList li").count();
      expect(count).toBeGreaterThan(0);
      await page.locator("#jobList").getByRole("button", { name: "Mark applied" }).first().click();
      await expect(page.locator("#kpi")).toContainText("1 applied this week");
      await page.locator("#statusFilter").selectOption("applied");
      await expect(page.locator("#jobCount")).toContainText("1 role");
      await page.keyboard.press("Tab");
      expect(bodies.join("\n")).not.toContain("Ada Lovelace");
      expect(bad.filter((line) => !/favicon/i.test(line))).toEqual([]);

      const axe = await new AxeBuilder({ page }).analyze();
      const serious = axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical");
      expect(serious).toEqual([]);
    });
  });
}

test("opens from cache when the network is off", async ({ page, context }) => {
  await page.goto("./");
  await page.evaluate(async () => {
    const ready = await navigator.serviceWorker.ready;
    await ready.update().catch(() => {});
  });
  await context.setOffline(true);
  await page.reload();
  await expect(page.locator("h1").first()).toBeVisible();
});
