import { expect, test } from "@playwright/test";
import { stubSearch } from "./stub-search.js";

test.use({ serviceWorkers: "block" });

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

test("fifty saves and twenty status changes stay under 5 MB", async ({ page }) => {
  test.setTimeout(120000);
  const posted = new Date().toISOString();
  const jobs = Array.from({ length: 50 }, (_, index) => ({
    id: `gh-long-${index}`,
    title: "Machine Learning Engineer",
    company: `Northwind ${index}`,
    location_raw: "United States",
    country: "united-states",
    family: "software",
    posted_at: posted,
    url: `https://example.com/jobs/${index}`,
    description_text: `Python PyTorch machine learning role ${index}. Statistics, SQL, and production model training for posting ${index}.`,
    embedding: Array(384).fill(0.01),
  }));
  await page.addInitScript(() => {
    localStorage.setItem("jobAutopilotSetup", JSON.stringify({
      name: "Ada",
      resume_name: "resume.txt",
      resume_text: "Machine learning engineer. Python, PyTorch, SQL, and statistics.",
      skills: ["python", "pytorch", "sql"],
      roles: ["Machine Learning Engineer"],
      locations: ["United States"],
      lookback_days: 14,
      runs_per_day: 0,
    }));
    localStorage.setItem("jobAutopilotWhatsNew", "folio-1");
    localStorage.removeItem("jobAutopilotJobs");
  });
  await stubSearch(page, jobs);
  await page.goto("./");
  await page.locator("#btnSearch").click();
  await expect(page.locator("#statusLive")).toContainText("Found 50 roles", { timeout: 20000 });
  for (let index = 0; index < 50; index += 1) {
    await page.getByRole("button", { name: "Save", exact: true }).first().click();
    await expect(page.getByRole("button", { name: "Saved", exact: true })).toHaveCount(index + 1);
  }
  const tracked = async () => page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open("job-autopilot");
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
    const rows = await new Promise((resolve, reject) => {
      const request = db.transaction("jobs").objectStore("jobs").getAll();
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result || []);
    });
    db.close();
    return rows.map((row) => row.status);
  });
  expect(await tracked()).toHaveLength(50);
  await page.keyboard.press("g");
  await page.keyboard.press("t");
  const saved = page.locator("#board section", { hasText: /^saved/ }).locator("article select");
  for (let index = 0; index < 20; index += 1) {
    await saved.first().selectOption(index < 10 ? "applied" : "interview");
  }
  await page.reload();
  const statuses = await tracked();
  expect(statuses).toHaveLength(50);
  expect(statuses.filter((status) => status === "applied")).toHaveLength(10);
  expect(statuses.filter((status) => status === "interview")).toHaveLength(10);
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
});
