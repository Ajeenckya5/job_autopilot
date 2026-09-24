import { describe, expect, it } from "vitest";
import { absorbResume, RESUME_TEXT_LIMIT } from "../src/logic/profile.js";
import { countriesFor, familiesFor, SEARCH_LIMIT, searchQuery } from "../src/logic/search.js";

describe("worker search query", () => {
  it("filters by place, look-back, and role family with a 300 cap", () => {
    const now = Date.parse("2026-09-23T00:00:00Z");
    const query = searchQuery({
      locations: ["United States", "Remote"],
      roles: ["Software Engineer", "Registered Nurse"],
      lookback_days: 14,
    }, now);
    expect(query.countries).toEqual(["united-states", "remote"]);
    expect(query.families).toEqual(["software", "health"]);
    expect(query.since).toBe("2026-09-09T00:00:00.000Z");
    expect(query.limit).toBe(SEARCH_LIMIT);
    expect(countriesFor(["Remote"])).toEqual(["remote"]);
    expect(familiesFor(["Warehouse associate"])).toEqual(["logistics"]);
  });
});

describe("stored profile", () => {
  it("keeps resume text under 20 KB and drops the file", () => {
    const stored = absorbResume({
      name: "Ada",
      resume_name: "resume.txt",
      resume_file: "raw-bytes",
      resume_text: "Ada Lovelace\nSoftware engineer\n2018 - 2026\nPython and SQL. ".repeat(800),
      roles: ["Software engineer"],
      locations: ["United States"],
    });
    expect(stored.resume_file).toBeUndefined();
    expect(stored.resume_text.length).toBeLessThanOrEqual(RESUME_TEXT_LIMIT);
    expect(stored.resume_text).toContain("Software engineer");
    expect(stored.skills).toContain("python");
    expect(stored.titles.length).toBeGreaterThan(0);
    expect(stored.years).toBeGreaterThan(0);
  });
});
