import { describe, expect, it } from "vitest";
import { absorbResume, RESUME_TEXT_LIMIT } from "../src/logic/profile.js";
import { countriesFor, SEARCH_LIMIT, searchQuery, searchTermsFor, skillTermsFor } from "../src/logic/search.js";

describe("worker search query", () => {
  it("asks by place, look-back, titles and resume skills with a 300 cap, for any field", () => {
    const now = Date.parse("2026-09-23T00:00:00Z");
    const query = searchQuery({
      locations: ["United States", "Remote"],
      roles: ["Software Engineer", "Registered Nurse"],
      lookback_days: 14,
    }, now);
    expect(query.countries).toEqual(["united-states", "remote"]);
    expect(query.families).toBeUndefined();
    expect(query.terms.slice(0, 2)).toEqual(["software engineer", "registered nurse"]);
    expect(query.since).toBe("2026-09-09T00:00:00.000Z");
    expect(query.limit).toBe(SEARCH_LIMIT);
    expect(countriesFor(["Remote"])).toEqual(["remote"]);
  });

  it("searches fields no list ever named", () => {
    const profile = {
      roles: ["Paralegal"],
      resume_text: "Litigation Paralegal\n2019 - 2026\n- Drafted pleadings and discovery responses; e-discovery in Relativity",
    };
    expect(searchTermsFor(profile)).toContain("paralegal");
    const skills = skillTermsFor(profile);
    expect(skills).toContain("pleadings");
    expect(skills.every((term) => term.length >= 4)).toBe(true);
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
    expect(stored.skills).toEqual(expect.arrayContaining(["Python", "SQL"]));
    expect(stored.titles).toContain("Software engineer");
    expect(stored.years).toBeGreaterThan(0);
  });
});
