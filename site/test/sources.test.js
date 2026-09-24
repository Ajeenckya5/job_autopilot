import { describe, expect, it } from "vitest";
import { guestAllowed, mergeSources, noteGuestResponse, parseAlertHtml, planSerpQueries } from "../src/lib/sources.js";
import { parseGoogleJobs, parseJobright, parseLinkedIn } from "../../extension/parsers.js";

describe("source merge", () => {
  it("keeps one card when LinkedIn and a company board share a title", () => {
    const merged = mergeSources([
      { source: "linkedin", jobs: [{ id: "li-1", title: "Software engineer", company: "Northwind", url: "https://www.linkedin.com/jobs/view/99?trk=public" }] },
      { source: "greenhouse", jobs: [{ id: "gh-1", title: "Software engineer", company: "Northwind", url: "https://boards.greenhouse.io/northwind/jobs/1", description_text: "Python" }] },
    ]);
    expect(merged).toHaveLength(1);
    expect(merged[0].seen_on.sort()).toEqual(["greenhouse", "linkedin"]);
    expect(merged[0].url).toContain("greenhouse.io");
    expect(merged[0].url).not.toContain("trk=");
  });
});

describe("serpapi budget", () => {
  it("stops at 90 percent of the monthly quota", () => {
    expect(planSerpQueries(["Engineer"], ["Chicago"], 225)).toEqual([]);
    expect(planSerpQueries(["Engineer", "Nurse"], ["Chicago"], 0)).toHaveLength(2);
  });
});

describe("linkedin guest search", () => {
  it("stays off until enabled and pauses for a day after a block", () => {
    expect(guestAllowed({ blockedUntil: 0 }, false)).toBe(false);
    expect(guestAllowed({ blockedUntil: 0 }, true)).toBe(true);
    const blocked = noteGuestResponse(429, 1_000);
    expect(guestAllowed(blocked, true, 1_000 + 1000)).toBe(false);
    expect(guestAllowed(blocked, true, blocked.blockedUntil + 1)).toBe(true);
  });
});

describe("alert email", () => {
  it("keeps job links and drops the rest of the message", () => {
    const html = '<a href="https://www.linkedin.com/jobs/view/42?trk=eml&amp;utm_source=email">Role</a> Hello Ada';
    const jobs = parseAlertHtml(html);
    expect(jobs).toHaveLength(1);
    expect(jobs[0].external_ids.linkedin).toBe("42");
    expect(jobs[0].url).not.toContain("trk=");
    expect(JSON.stringify(jobs)).not.toContain("Ada");
  });
});

describe("page capture", () => {
  it("reads linkedin, google jobs, and jobright cards from saved html", () => {
    const linkedin = parseLinkedIn('<a class="base-search-card__title" href="/jobs/view/7"> Nurse </a><h4 class="base-search-card__subtitle"> Clinic </h4>');
    expect(linkedin[0].external_ids.linkedin).toBe("7");
    expect(linkedin[0].confidence).toBeGreaterThanOrEqual(0.8);
    const google = parseGoogleJobs('<article data-job="Teacher|School|Chicago"></article>');
    expect(google[0].source).toBe("google_jobs");
    const jobright = parseJobright('<a href="https://jobright.ai/jobs/info/abc" data-title="Analyst">x</a>');
    expect(jobright[0].title).toBe("Analyst");
  });
});
