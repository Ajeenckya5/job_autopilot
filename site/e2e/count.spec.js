import { expect, test } from "@playwright/test";

test("the count matches the rows when the location is Remote", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const posted = new Date().toISOString();
  await page.addInitScript((when) => {
    localStorage.setItem("jobAutopilotSetup", JSON.stringify({
      name: "Ada",
      resume_name: "resume.txt",
      resume_text: "Machine learning engineer and data scientist. Python and PyTorch.",
      roles: ["Machine Learning Engineer", "Data Scientist"],
      locations: ["Remote"],
      lookback_days: 30,
    }));
    localStorage.setItem("jobAutopilotJobs", JSON.stringify([
      { id: "ml", title: "Machine Learning Engineer", company: "Northwind", location_raw: "Remote", remote_type: "remote", posted_at: when, status: "new", url: "https://example.com/ml", description_text: "Python PyTorch" },
      { id: "ds", title: "Applied Scientist", company: "Northwind", location_raw: "Remote", remote_type: "remote", posted_at: when, status: "new", url: "https://example.com/ds", description_text: "experiments" },
      { id: "ops", title: "Senior DevOps Engineer", company: "1X", location_raw: "Remote", remote_type: "remote", posted_at: when, status: "new", url: "https://example.com/ops", description_text: "kubernetes" },
    ]));
  }, posted);
  await page.goto("./#home");
  const count = Number((await page.locator("#jobCount").innerText()).match(/\d+/)[0]);
  const found = Number((await page.locator("#statusLive").innerText()).match(/\d+/)[0]);
  const rows = await page.locator("#jobList li:not(.empty)").count();
  expect(count).toBe(rows);
  expect(found).toBe(rows);
  await expect(page.locator("#jobList")).not.toContainText("DevOps");
});
