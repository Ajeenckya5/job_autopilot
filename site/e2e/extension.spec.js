import { expect, test, chromium } from "@playwright/test";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

const extension = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../extension");

test("imports jobs from fixture pages without typing an extension id", async ({ browserName }) => {
  test.skip(browserName !== "chromium", "The capture extension runs in Chrome.");
  const context = await chromium.launchPersistentContext(path.join(os.tmpdir(), `job-ext-${Date.now()}`), {
    channel: "chrome",
    headless: false,
    ignoreDefaultArgs: ["--disable-extensions"],
    args: [
      `--disable-extensions-except=${extension}`,
      `--load-extension=${extension}`,
    ],
  });
  const page = await context.newPage();
  for (const name of ["linkedin", "google", "jobright"]) {
    await page.goto(`http://127.0.0.1:4174/fixtures/${name}.html`);
    await page.locator("#job-autopilot-save").click();
    await expect(page.locator("#job-autopilot-save")).toContainText("Saved");
  }
  await page.addInitScript(() => {
    localStorage.setItem("jobAutopilotSetup", JSON.stringify({
      name: "Ada",
      resume_name: "resume.txt",
      resume_text: "Machine learning engineer.",
      roles: ["Machine Learning Engineer"],
      locations: ["Remote"],
      lookback_days: 30,
    }));
  });
  await page.goto("http://127.0.0.1:4174/#home");
  await expect(page.locator("#toast")).toContainText(/Imported 3 jobs from/);
  await expect(page.locator("#extensionId")).toHaveValue("");
  await context.close();
});
