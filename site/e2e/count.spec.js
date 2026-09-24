import { expect, test } from "@playwright/test";
import { sampleJobs, stubSearch } from "./stub-search.js";

test.use({ serviceWorkers: "block" });

test("the count matches the rows when the location is Remote", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    localStorage.setItem("jobAutopilotSetup", JSON.stringify({
      name: "Ada",
      resume_name: "resume.txt",
      skills: ["python", "pytorch"],
      titles: ["Machine Learning Engineer"],
      years: 6,
      roles: ["Machine Learning Engineer", "Data Scientist"],
      locations: ["Remote"],
      lookback_days: 30,
    }));
  });
  await stubSearch(page, sampleJobs().filter((job) => /remote/i.test(job.location_raw)));
  await page.goto("./#home");
  await page.locator("#btnSearch").click();
  await expect(page.locator("#statusLive")).toContainText(/Found [1-9]/, { timeout: 20000 });
  const count = Number((await page.locator("#jobCount").innerText()).match(/\d+/)[0]);
  const found = Number((await page.locator("#statusLive").innerText()).match(/\d+/)[0]);
  const rows = await page.locator("#jobList li.job").count();
  expect(count).toBe(rows);
  expect(found).toBe(rows);
  await expect(page.locator("#jobList")).not.toContainText("DevOps");
});
