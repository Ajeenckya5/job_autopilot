import { readFileSync } from "fs";
import { describe, expect, it } from "vitest";
import { rankAll, selectJobs } from "../src/logic/jobs.js";

const pairs = JSON.parse(readFileSync(new URL("./fixtures/labelled_jobs.json", import.meta.url)));
const now = Date.parse("2026-09-23T12:00:00Z");

function job(id, row) {
  return {
    id,
    title: row.title,
    company: "Northwind",
    location_raw: "United States",
    locations: [{ country: "United States" }],
    posted_at: "2026-09-22T00:00:00Z",
    description_text: row.description,
    url: `https://example.com/${id}`,
    status: "new",
  };
}

describe("labelled pairs", () => {
  it("keeps a 20 point gap and 0.9 precision across 8 families", () => {
    expect(pairs.length).toBeGreaterThanOrEqual(600);
    expect(new Set(pairs.map((pair) => pair.family)).size).toBe(8);
    let shown = 0;
    let shownRelevant = 0;
    pairs.forEach((pair, index) => {
      const ranked = rankAll([
        job(`${index}-rel`, pair.relevant),
        job(`${index}-irr`, pair.irrelevant),
      ], {
        resume_text: pair.resume,
        roles: pair.roles,
        locations: ["United States"],
        lookback_days: 30,
      }, now);
      const relevant = ranked.find((item) => item.id.endsWith("-rel"));
      const irrelevant = ranked.find((item) => item.id.endsWith("-irr"));
      expect(relevant.match_score - irrelevant.match_score).toBeGreaterThanOrEqual(20);
      ranked.filter((item) => item.bucket !== "possible").forEach((item) => {
        shown += 1;
        if (item.id.endsWith("-rel")) shownRelevant += 1;
      });
    });
    expect(shownRelevant / shown).toBeGreaterThanOrEqual(0.9);
  });

  it("ranks an exact target above an adjacent role when the skills match", () => {
    const description = "Python PyTorch machine learning experiments and SQL.";
    const ranked = rankAll([
      ["arch", "Data Architect"],
      ["ml", "Staff Software Engineer, Machine Learning"],
      ["applied", "Applied Scientist"],
      ["de", "Data Engineer"],
    ].map(([id, title]) => job(id, { title, description })), {
      resume_text: "Machine learning engineer and data scientist. Python and PyTorch.",
      roles: ["Machine Learning Engineer", "Data Scientist"],
      locations: ["United States"],
      lookback_days: 30,
    }, now);
    const score = (id) => ranked.find((item) => item.id === id).match_score;
    expect(score("ml")).toBeGreaterThan(score("applied"));
    expect(score("ml")).toBeGreaterThan(score("de"));
    expect(score("ml")).toBeGreaterThan(score("arch"));
    expect(ranked.find((item) => item.id === "applied").relation).toMatch(/Related|Similar title/);
  });

  it("does not put DevOps, Sales, or Systems in the top 10 for an ML resume", () => {
    const titles = [
      "Senior DevOps Engineer",
      "Sales Manager",
      "Systems Engineer",
      "Machine Learning Engineer",
      "Data Scientist",
      "Applied Scientist",
      "ML Engineer",
      "Research Scientist",
      "Warehouse Associate",
      "Account Executive",
      "Senior Machine Learning Engineer",
      "Staff Data Scientist",
    ];
    const ranked = rankAll(titles.map((title, index) => job(String(index), {
      title,
      description: title.includes("Machine") || title.includes("Data") || title.includes("Scientist") || title.includes("ML")
        ? "Python PyTorch machine learning experiments"
        : "Unrelated duties",
    })), {
      resume_text: "Machine learning engineer and data scientist. Python and PyTorch.",
      roles: ["Machine Learning Engineer", "Data Scientist"],
      locations: ["United States", "Remote"],
      lookback_days: 30,
    }, now).filter((item) => item.bucket !== "possible");
    const top = ranked.slice(0, 10).map((item) => item.title);
    expect(top.length).toBeGreaterThan(0);
    expect(top.some((title) => /devops|sales|systems/i.test(title))).toBe(false);
  });
});

describe("one filter for the count and the list", () => {
  const posted = "2026-09-22T00:00:00Z";
  const profile = {
    resume_text: "Machine learning engineer and data scientist.",
    roles: ["Machine Learning Engineer", "Data Scientist"],
    locations: ["Remote"],
    lookback_days: 7,
  };

  it("uses the same rows for the count and the rendered list", () => {
    const jobs = [
      job("ml", { title: "Machine Learning Engineer", description: "Python models" }),
      job("ops", { title: "Senior DevOps Engineer", description: "Kubernetes" }),
      job("sales", { title: "Sales Manager", description: "Quota" }),
    ].map((item) => ({ ...item, location_raw: "Remote", remote_type: "remote", posted_at: posted }));
    const selected = selectJobs(jobs, profile, {}, now);
    expect(selected.rows).toHaveLength(1);
    expect(selected.rows[0].title).toBe("Machine Learning Engineer");
    expect(selected.rows.some((item) => /devops|sales/i.test(item.title))).toBe(false);
  });

  it("names the look-back when that filter hides every role", () => {
    const jobs = [job("old", { title: "Machine Learning Engineer", description: "Python" })]
      .map((item) => ({ ...item, posted_at: "2026-01-01T00:00:00Z", location_raw: "Remote" }));
    const selected = selectJobs(jobs, profile, {}, now);
    expect(selected.rows).toHaveLength(0);
    expect(selected.reasons[0].text).toBe("1 role hidden by look-back of 7 days");
    expect(selected.reasons[0].fix).toBe("widen");
  });
});
