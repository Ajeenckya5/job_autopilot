import { expect, test } from "@playwright/test";
import { stubSearch } from "./stub-search.js";

const BUDGET = 5 * 1024 * 1024;

test("a full session stays under 5 MB of browser storage", async ({ page }) => {
  const shards = [];
  page.on("request", (request) => {
    if (request.url().includes("/feeds/")) shards.push(request.url());
  });
  await stubSearch(page);
  await page.goto("./");
  await page.locator("#resumeFile").setInputFiles({
    name: "resume.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Ada Lovelace\nSoftware engineer with Python, JavaScript, SQL, and AWS experience building production systems."),
  });
  await expect(page.locator("#stepPrefs")).toBeVisible();
  await page.locator("#personName").fill("Ada Lovelace");
  await page.locator("#roleInput").fill("Software engineer");
  await page.locator("#whereInput").fill("United States");
  await page.locator("#runs").fill("1");
  await page.locator("#saveSetup").click();
  await expect(page.locator("#statusLive")).toContainText(/Found|feed|role/i, { timeout: 20000 });
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.locator("#storageUsed")).toContainText(/Storage used: \d+\.\d MB/);
  await expect(page.locator("#aiConsentLead")).toContainText("contact details removed");
  await expect(page.locator("#aiExact")).toBeVisible();
  const bytes = await page.evaluate(async () => {
    const estimate = navigator.storage?.estimate ? await navigator.storage.estimate() : { usage: 0 };
    let local = 0;
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index) || "";
      local += (key.length + String(localStorage.getItem(key) || "").length) * 2;
    }
    const usage = Number(estimate.usage) || 0;
    return usage < local ? usage + local : usage;
  });
  expect(bytes).toBeLessThanOrEqual(BUDGET);
  expect(shards).toEqual([]);
  const setup = JSON.parse(await page.evaluate(() => localStorage.getItem("jobAutopilotSetup") || "{}"));
  expect(setup.resume_file).toBeUndefined();
  expect(String(setup.resume_text || "").length).toBeLessThanOrEqual(20 * 1024);
  expect(setup.resume_text).toContain("Software engineer");
});
