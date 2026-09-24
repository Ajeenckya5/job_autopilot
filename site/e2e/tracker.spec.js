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

async function savedRows(page) {
  return page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open("job-autopilot");
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
    const rows = await new Promise((resolve, reject) => {
      const request = db.transaction("jobs", "readonly").objectStore("jobs").getAll();
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result || []);
    });
    db.close();
    return rows.map((row) => ({
      title: row.title,
      status: row.status,
      notes: row.notes || "",
      applied_at: row.applied_at || "",
      interview_at: row.interview_at || "",
      updated_at: row.updated_at || "",
    }));
  });
}

async function openHome(page) {
  await page.addInitScript((data) => {
    localStorage.setItem("jobAutopilotSetup", JSON.stringify(data));
    localStorage.setItem("jobAutopilotWhatsNew", "folio-1");
    localStorage.removeItem("jobAutopilotJobs");
  }, setup);
  await stubSearch(page);
  await page.goto("./");
  await page.locator("#btnSearch").click();
  await expect(page.locator("#statusLive")).toContainText(/Found [1-9]/, { timeout: 20000 });
}

test("status, notes, and dates survive reload and a new tab", async ({ page, context }) => {
  await openHome(page);
  const title = await page.locator("#jobList li.job h3").first().innerText();
  await page.locator("#jobList li.job").first().getByRole("button", { name: "Mark applied" }).click();
  await page.keyboard.press("g");
  await page.keyboard.press("t");
  const card = page.locator("#board article", { hasText: title });
  await expect(card).toBeVisible();
  await card.getByPlaceholder("Notes").fill("Ask about the ranking work");
  await card.getByPlaceholder("Notes").blur();
  await card.getByLabel(`Status for ${title}`).selectOption("interview");
  await page.reload();
  const again = page.locator("#board article", { hasText: title });
  await expect(again).toBeVisible();
  await expect(again.getByLabel(`Status for ${title}`)).toHaveValue("interview");
  await expect(again.getByPlaceholder("Notes")).toHaveValue("Ask about the ranking work");
  const stored = (await savedRows(page)).find((row) => row.title === title);
  expect(stored.status).toBe("interview");
  expect(stored.notes).toBe("Ask about the ranking work");
  expect(stored.applied_at).not.toBe("");
  expect(stored.interview_at).not.toBe("");
  expect(stored.updated_at).not.toBe("");

  await page.close();
  const next = await context.newPage();
  await next.goto("./#tracker");
  const moved = next.locator("#board article", { hasText: title });
  await expect(moved).toBeVisible();
  await expect(moved.getByLabel(`Status for ${title}`)).toHaveValue("interview");
  await expect(moved.getByPlaceholder("Notes")).toHaveValue("Ask about the ranking work");
});

test("rapid saves and a save immediately before reload are kept", async ({ page }) => {
  await openHome(page);
  const title = await page.locator("#jobList li.job h3").first().innerText();
  await page.locator("#jobList li.job").first().getByRole("button", { name: "Save" }).click();
  await page.keyboard.press("g");
  await page.keyboard.press("t");
  const card = page.locator("#board article", { hasText: title });
  await expect(card).toBeVisible();
  await card.getByLabel(`Status for ${title}`).selectOption("applied");
  await card.getByLabel(`Status for ${title}`).selectOption("interview");
  await card.getByLabel(`Status for ${title}`).selectOption("offer");
  await card.getByPlaceholder("Notes").fill("Panel on Thursday");
  await card.getByPlaceholder("Notes").blur();
  await page.reload();
  const again = page.locator("#board article", { hasText: title });
  await expect(again).toBeVisible();
  await expect(again.getByLabel(`Status for ${title}`)).toHaveValue("offer");
  await expect(again.getByPlaceholder("Notes")).toHaveValue("Panel on Thursday");
  const stored = (await savedRows(page)).find((row) => row.title === title);
  expect(stored.status).toBe("offer");
  expect(stored.notes).toBe("Panel on Thursday");
  expect(stored.applied_at).not.toBe("");
  expect(stored.interview_at).not.toBe("");
});
