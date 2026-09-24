import { describe, expect, it } from "vitest";
import { parseRunsPerDay, plural, safeHref } from "../src/logic/text.js";
import { applySearchResult, collapsePostings, locationOk, rankAll, selectJobs, skillHits, sponsorshipOf, withinLookback, yearsRequired } from "../src/logic/jobs.js";
import { shardsToFetch } from "../src/logic/feed.js";
import { clampLookback as look } from "../src/logic/text.js";
import { filterJobs, kpis, markJob } from "../src/logic/tracker.js";
import { readResumeFile, skillsFromText } from "../src/logic/resume.js";

describe("safe links", () => {
  it("drops javascript urls", () => {
    expect(safeHref("javascript:alert(1)")).toBe("");
    expect(safeHref("https://example.com/jobs/1")).toBe("https://example.com/jobs/1");
  });
});

describe("schedule input", () => {
  it("rejects fractional runs per day", () => {
    expect(parseRunsPerDay("2.5").ok).toBe(false);
    expect(parseRunsPerDay("4").ok).toBe(true);
  });
});

describe("history", () => {
  it("keeps earlier jobs when a search is blocked", () => {
    const prev = [{ id: "a", title: "Kept", status: "applied" }];
    expect(applySearchResult(prev, { ok: false, jobs: [] })).toEqual(prev);
  });
  it("merges a new search without dropping status", () => {
    const prev = [{ id: "a", title: "Old", status: "interview", notes: "panel" }];
    const next = applySearchResult(prev, { ok: true, jobs: [{ id: "a", title: "Old", status: "new" }, { id: "b", title: "New" }] });
    expect(next.find((j) => j.id === "a").status).toBe("interview");
    expect(next.find((j) => j.id === "b")).toBeTruthy();
  });
});

describe("locations and look-back", () => {
  it("accepts a job in any chosen place", () => {
    expect(locationOk(["Austin, TX"], ["Chicago", "Austin"])).toBe(true);
    expect(locationOk(["Berlin, Germany"], ["United States"])).toBe(false);
  });
  it("keeps a 7 day look-back", () => {
    expect(look(7)).toBe(7);
    const now = Date.parse("2026-09-22T00:00:00Z");
    expect(withinLookback("2026-09-20T00:00:00Z", 7, now)).toBe(true);
    expect(withinLookback("2026-08-01T00:00:00Z", 7, now)).toBe(false);
  });
});

describe("requirements", () => {
  it("reads years and sponsorship from the posting", () => {
    expect(yearsRequired("Engineer", "3+ years of Python")).toBe(3);
    expect(sponsorshipOf("We are unable to sponsor visas")).toBe("no");
    expect(sponsorshipOf("Visa sponsorship is available")).toBe("yes");
  });
  it("drops jobs below the salary floor and leadership titles when asked", () => {
    const now = Date.parse("2026-09-22T12:00:00Z");
    const profile = {
      resume_text: "Software engineer with Python.",
      roles: ["Software engineer"],
      locations: ["United States"],
      lookback_days: 14,
      min_salary: 100000,
      skip_leadership: true,
    };
    const ranked = rankAll([
      { id: "low", title: "Software engineer", location_raw: "United States", posted_at: "2026-09-21T00:00:00Z", salary_max: 40000, description_text: "Python software engineer", url: "https://example.com/a" },
      { id: "lead", title: "Director of engineering", location_raw: "United States", posted_at: "2026-09-21T00:00:00Z", salary_max: 200000, description_text: "Python", url: "https://example.com/b" },
      { id: "ok", title: "Software engineer", location_raw: "Remote, United States", posted_at: "2026-09-21T00:00:00Z", salary_max: 150000, description_text: "Python software engineer", url: "https://example.com/c" },
    ], profile, now);
    expect(ranked.map((job) => job.id)).toEqual(["ok"]);
  });
});

describe("scores", () => {
  const now = Date.parse("2026-09-22T12:00:00Z");
  const profile = {
    resume_text: "Machine learning engineer with Python, PyTorch, and NLP research.",
    roles: ["Machine learning engineer"],
    locations: ["United States"],
    lookback_days: 14,
    max_years: 5,
  };
  const jobs = [
    {
      id: "good",
      title: "Machine learning engineer",
      company: "Northwind",
      location_raw: "United States",
      locations: [{ country: "United States" }],
      posted_at: "2026-09-21T00:00:00Z",
      years_required: 2,
      description_text: "Build NLP models in Python and PyTorch.",
      url: "https://example.com/good",
    },
    {
      id: "bad",
      title: "Warehouse associate",
      company: "Pallets",
      location_raw: "United States",
      locations: [{ country: "United States" }],
      posted_at: "2026-09-21T00:00:00Z",
      years_required: 0,
      description_text: "Operate a forklift and move shipping pallets.",
      url: "https://example.com/bad",
    },
    {
      id: "senior",
      title: "Staff engineer",
      company: "Old",
      location_raw: "United States",
      posted_at: "2026-09-21T00:00:00Z",
      years_required: 12,
      description_text: "12+ years required. Python.",
      url: "https://example.com/senior",
    },
    {
      id: "abroad",
      title: "Machine learning engineer",
      company: "Berlin Lab",
      location_raw: "Berlin, Germany",
      posted_at: "2026-09-21T00:00:00Z",
      description_text: "Python PyTorch NLP",
      url: "https://example.com/de",
    },
  ];
  it("separates relevant and irrelevant jobs by at least 20 points", () => {
    const ranked = rankAll(jobs, profile, now);
    const good = ranked.find((j) => j.id === "good");
    const bad = ranked.find((j) => j.id === "bad");
    expect(good).toBeTruthy();
    expect(bad).toBeTruthy();
    expect(good.match_score).toBeGreaterThanOrEqual(80);
    expect(good.why_missing.join(" ")).not.toMatch(/engineer|models|build/);
    expect(good.match_score - bad.match_score).toBeGreaterThanOrEqual(20);
    expect(ranked.find((j) => j.id === "senior")).toBeUndefined();
    expect(ranked.find((j) => j.id === "abroad")).toBeUndefined();
  });
});

describe("skills, cards, and look-back", () => {
  it("lists missing skills from the dictionary only", () => {
    const hits = skillHits("Python and PyTorch.", "Machine learning engineer. Python. Must know SQL and nursing.");
    expect(hits.matched).toContain("python");
    expect(hits.missing).toContain("sql");
    expect(hits.missing).toContain("machine learning");
    expect(hits.missing).not.toContain("engineer");
  });

  it("merges the same posting across cities", () => {
    const desc = "Care for patients in clinic. ".repeat(8);
    const rows = collapsePostings([
      { id: "a", source: "greenhouse", company: "One Medical ", title: "Family Nurse Practitioner", location_raw: "Chicago, IL", description_text: desc, url: "https://example.com/a" },
      { id: "b", source: "greenhouse", company: "One Medical", title: "Family Nurse Practitioner", location_raw: "Austin, TX", description_text: desc, url: "https://example.com/b" },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].company).toBe("One Medical");
    expect(rows[0].location_raw).toContain("Chicago, IL");
    expect(rows[0].location_raw).toContain("Austin, TX");
  });

  it("merges the same posting across sources", () => {
    const desc = "Build ranking models in Python. ".repeat(6);
    const rows = collapsePostings([
      { id: "a", source: "greenhouse", company: "Northwind", title: "Machine Learning Engineer", location_raw: "Remote", description_text: desc, url: "https://boards.example/a" },
      { id: "b", source: "lever", company: "Northwind", title: "Machine Learning Engineer", location_raw: "United States", description_text: desc, url: "https://jobs.example/b" },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].sources).toEqual(["greenhouse", "lever"]);
    expect(rows[0].location_raw).toContain("Remote");
    expect(rows[0].location_raw).toContain("United States");
  });

  it("says how many roles a 30 day look-back would add", () => {
    const now = Date.parse("2026-09-22T12:00:00Z");
    const profile = {
      resume_text: "Machine learning engineer. Python and PyTorch.",
      roles: ["Machine Learning Engineer"],
      locations: ["Remote"],
      lookback_days: 14,
    };
    const jobs = [1, 2, 3, 4, 5].map((n) => ({
      id: `new-${n}`,
      title: `Machine Learning Engineer ${n}`,
      company: "Northwind",
      location_raw: "Remote",
      posted_at: "2026-09-20T00:00:00Z",
      description_text: "Python and PyTorch models for production.",
      url: `https://example.com/new-${n}`,
    })).concat([1, 2, 3].map((n) => ({
      id: `old-${n}`,
      title: `Machine Learning Engineer older ${n}`,
      company: "Northwind",
      location_raw: "Remote",
      posted_at: "2026-09-02T00:00:00Z",
      description_text: "Python and PyTorch models for production research.",
      url: `https://example.com/old-${n}`,
    })));
    const selected = selectJobs(jobs, profile, {}, now);
    expect(selected.rows.length).toBeLessThan(20);
    expect(selected.rows).toHaveLength(5);
    const widen = selected.reasons.find((reason) => reason.id === "widen-more");
    expect(widen.added).toBe(3);
    expect(widen.text).toContain("Look back 30 days");
  });

  it("fetches a shard only when its hash changed", () => {
    const manifest = { shards: ["a.json", "b.json"], sha256: { "a.json": "aaa", "b.json": "bbb" } };
    expect(shardsToFetch(manifest, { "a.json": "aaa", "b.json": "old" })).toEqual(["b.json"]);
    expect(shardsToFetch(manifest, { "a.json": "aaa", "b.json": "bbb" })).toEqual([]);
  });
});

describe("tracker", () => {
  it("filters and counts applied jobs", () => {
    let job = { id: "1", title: "Nurse", company: "Clinic", status: "new" };
    job = markJob(job, "applied", "2026-09-21T00:00:00Z");
    expect(job.status).toBe("applied");
    expect(filterJobs([job], { status: "applied", q: "nur" })).toHaveLength(1);
    expect(filterJobs([job], { q: "warehouse" })).toHaveLength(0);
    const stats = kpis([job], Date.parse("2026-09-22T00:00:00Z"));
    expect(stats.applied_week).toBe(1);
  });
});

describe("resume", () => {
  it("reads skills and refuses legacy doc files", async () => {
    expect(skillsFromText("Python and PyTorch")).toContain("python");
    const doc = { name: "cv.doc", text: async () => "" };
    await expect(readResumeFile(doc)).rejects.toThrow(/DOCX/);
  });
  it("errors when a PDF has no text layer", async () => {
    const file = { name: "scan.pdf", type: "application/pdf", arrayBuffer: async () => new ArrayBuffer(8) };
    const pdfjs = {
      getDocument() {
        return { promise: Promise.resolve({ numPages: 1, getPage: async () => ({ getTextContent: async () => ({ items: [] }) }) }) };
      },
    };
    await expect(readResumeFile(file, { pdfjs })).rejects.toThrow(/no text layer/);
  });
  it("reads a PDF text layer", async () => {
    const file = { name: "cv.pdf", type: "application/pdf", arrayBuffer: async () => new ArrayBuffer(8) };
    const pdfjs = {
      getDocument() {
        return {
          promise: Promise.resolve({
            numPages: 1,
            getPage: async () => ({
              getTextContent: async () => ({ items: [{ str: "Ada Lovelace Python machine learning engineer with PyTorch experience in production systems" }] }),
            }),
          }),
        };
      },
    };
    const got = await readResumeFile(file, { pdfjs });
    expect(got.skills).toContain("python");
  });
});

describe("copy", () => {
  it("pluralizes job counts", () => {
    expect(plural(1, "job")).toBe("1 job");
    expect(plural(2, "job")).toBe("2 jobs");
  });
});
