import { expect, test } from "@playwright/test";
import { stubSearch } from "./stub-search.js";

test.use({ serviceWorkers: "block" });

const setup = {
  name: "Avery",
  resume_name: "resume.txt",
  resume_text: "Machine learning engineer and data scientist. Python, PyTorch, NLP, statistics, and SQL.",
  skills: ["python", "pytorch", "sql", "machine learning"],
  roles: ["Machine Learning Engineer", "Data Scientist"],
  locations: ["United States", "Remote"],
  lookback_days: 14,
  runs_per_day: 0,
};

async function search(page, width, height) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.addInitScript((data) => {
    localStorage.setItem("jobAutopilotSetup", JSON.stringify(data));
    localStorage.removeItem("jobAutopilotJobs");
  }, setup);
  await page.setViewportSize({ width, height });
  const shards = [];
  const posts = [];
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/feeds/") && url.endsWith(".json")) shards.push(url);
    if (request.method() === "POST") posts.push(request.postData() || "");
  });
  await stubSearch(page);
  await page.goto("./");
  const start = Date.now();
  await page.locator("#btnSearch").click();
  await expect(page.locator("#statusLive")).toContainText(/Found [1-9]/, { timeout: 20000 });
  const first = Date.now() - start;
  const status = await page.locator("#statusLive").innerText();
  expect(status).not.toMatch(/quota/i);
  expect(errors.join("\n")).not.toMatch(/quota/i);
  expect(await page.locator("#jobList li.job").count()).toBeGreaterThan(0);
  expect(await page.evaluate(() => localStorage.getItem("jobAutopilotJobs"))).toBeNull();
  const stored = JSON.parse(await page.evaluate(() => localStorage.getItem("jobAutopilotSetup") || "{}"));
  expect(stored.resume_file).toBeUndefined();
  expect(String(stored.resume_text || "").length).toBeLessThanOrEqual(20 * 1024);
  expect(posts.join("\n")).not.toMatch(/@|resume_file/);
  expect(shards.length).toBe(0);
  const downloaded = shards.length;
  shards.length = 0;
  const again = Date.now();
  await page.locator("#btnSearch").click();
  await expect(page.locator("#statusLive")).toContainText(/Found [1-9]/, { timeout: 20000 });
  return { first, repeat: Date.now() - again, downloaded, repeatDownloads: shards.length, status };
}

test("United States search stays under 10 seconds on a desktop", async ({ page }) => {
  const result = await search(page, 1280, 800);
  console.log(`desktop first ${result.first}ms repeat ${result.repeat}ms shards ${result.downloaded} status ${result.status}`);
  expect(result.first).toBeLessThan(10000);
  expect(result.repeat).toBeLessThan(10000);
  expect(result.repeatDownloads).toBe(0);
});

test("United States search stays under 10 seconds on a phone", async ({ page }) => {
  const result = await search(page, 390, 844);
  console.log(`mobile first ${result.first}ms repeat ${result.repeat}ms shards ${result.downloaded} status ${result.status}`);
  expect(result.first).toBeLessThan(10000);
  expect(result.repeat).toBeLessThan(10000);
  expect(result.repeatDownloads).toBe(0);
});
