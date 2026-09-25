import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { sampleJobs, stubSearch } from "./stub-search.js";

test.use({ serviceWorkers: "block" });

test("a short list says where the other roles went", async ({ page }) => {
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
  let asked = "";
  page.on("request", (request) => {
    if (request.url().includes("/v1/search")) asked = request.url();
  });
  await stubSearch(page, sampleJobs());
  await page.goto("./#home");
  await page.locator("#btnSearch").click();
  await expect(page.locator("#statusLive")).toContainText(/Found \d/, { timeout: 20000 });
  expect(new URL(asked).searchParams.getAll("term")).toContain("machine learning engineer");
  const why = page.locator("#whyNot");
  await expect(why).toBeVisible();
  await why.locator("summary").click();
  await expect(why).toContainText("The last search looked at 4 roles");
  await expect(why).toContainText("outside your places");
  const scan = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(scan.violations.map((v) => v.id)).toEqual([]);
});
