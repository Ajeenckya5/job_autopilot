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

test("shortcuts and the command palette work from the keyboard", async ({ page }) => {
  await page.addInitScript((data) => {
    localStorage.setItem("jobAutopilotSetup", JSON.stringify(data));
    localStorage.setItem("jobAutopilotWhatsNew", "folio-1");
    localStorage.removeItem("jobAutopilotJobs");
  }, setup);
  await stubSearch(page);
  await page.goto("./");
  await page.locator("#btnSearch").click();
  await expect(page.locator("#statusLive")).toContainText(/Found [1-9]/, { timeout: 20000 });
  const cards = page.locator("#jobList li.job");
  expect(await cards.count()).toBeGreaterThan(1);

  await page.keyboard.press("j");
  await expect(cards.nth(1)).toHaveClass(/picked/);
  await page.keyboard.press("k");
  await expect(cards.nth(0)).toHaveClass(/picked/);
  await expect(cards.nth(1)).not.toHaveClass(/picked/);

  await page.keyboard.press("s");
  await expect(cards.nth(0).getByRole("button", { name: "Saved" })).toBeVisible();
  await page.keyboard.press("a");
  await expect(cards.nth(0).getByRole("button", { name: "Applied" })).toBeVisible();

  const hiddenTitle = await cards.nth(1).locator("h3").innerText();
  await page.keyboard.press("j");
  await page.keyboard.press("h");
  await expect(page.locator("#jobList h3", { hasText: hiddenTitle })).toHaveCount(0);

  const unlikeTitle = await cards.nth(0).locator("h3").innerText();
  await page.keyboard.press("n");
  await expect(page.locator("#jobList h3", { hasText: unlikeTitle })).toHaveCount(0);

  await page.keyboard.press("/");
  await expect(page.locator("#jobSearch")).toBeFocused();
  await page.keyboard.press("Escape");

  await page.keyboard.press("g");
  await page.keyboard.press("t");
  await expect(page.locator("[data-view=tracker]")).toBeVisible();
  await expect(page.locator("[data-nav=tracker]")).toHaveAttribute("aria-current", "page");

  await page.keyboard.press("Shift+/");
  await expect(page.locator("#whatsNew")).toBeVisible();
  await page.keyboard.press("Escape");

  await page.keyboard.press("Control+k");
  await expect(page.locator("#palette")).toBeVisible();
  await expect(page.locator("#paletteInput")).toBeFocused();
  await page.keyboard.type("s");
  await page.keyboard.press("ArrowDown");
  await expect(page.locator("#paletteList button[aria-selected=true]")).toHaveText("Settings");
  await page.keyboard.press("Enter");
  await expect(page.locator("#palette")).toBeHidden();
  await expect(page.locator("[data-view=settings]")).toBeVisible();

  await page.keyboard.press("Control+k");
  await page.keyboard.type("Home");
  await page.keyboard.press("Enter");
  await expect(page.locator("[data-view=home]")).toBeVisible();
});
