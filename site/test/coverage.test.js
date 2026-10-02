import { describe, expect, it } from "vitest";
import { collapsePostings, normalizeTitle, selectJobs } from "../src/logic/jobs.js";
import { fetchCandidates, searchQuery, searchTermsFor } from "../src/logic/search.js";
import { htmlToText } from "../src/logic/text.js";

describe("near-duplicate postings", () => {
  it("treats spacing, dashes and brackets in a title as the same job", () => {
    expect(normalizeTitle("In Office RN- NY Licensed ")).toBe(normalizeTitle("In Office RN-NY Licensed"));
    expect(normalizeTitle("Senior QA Engineer - Web3 (")).toBe("senior qa engineer web3");
    expect(normalizeTitle("C++ / C# Engineer")).toBe("c++ c# engineer");
    expect(normalizeTitle("クラウドエンジニア")).not.toBe(normalizeTitle("車両テストエンジニア"));
  });

  it("collapses the One Medical RN pair into one card", () => {
    const desc = "&lt;div&gt;&lt;p&gt;Care for members in our New York office.&lt;/p&gt;&lt;/div&gt;".repeat(4);
    const rows = collapsePostings([
      { id: "a", source: "greenhouse", company: "One Medical", title: "In Office RN- NY Licensed ", description_text: desc, location_raw: "New York, NY" },
      { id: "b", source: "greenhouse", company: "One Medical", title: "In Office RN-NY Licensed ", description_text: desc, location_raw: "New York, NY" },
    ]);
    expect(rows).toHaveLength(1);
  });
});

describe("job text", () => {
  it("turns escaped board HTML into plain text and keeps line breaks", () => {
    const raw = "&lt;div class=&quot;intro&quot;&gt;&lt;p&gt;&lt;strong&gt;About&lt;/strong&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; SQL&lt;/li&gt;&lt;li&gt;3+ years&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;";
    expect(htmlToText(raw)).toBe("About\nPython & SQL\n3+ years");
    expect(htmlToText("Must have python.\nBenefits")).toBe("Must have python.\nBenefits");
    expect(htmlToText("<script>alert(1)</script>Hi &#8211; there")).toBe("Hi – there");
  });
});

describe("search coverage", () => {
  const profile = { roles: ["Machine Learning Engineer", "Data Scientist"], locations: ["United States", "Remote"], lookback_days: 14 };

  it("asks for the role titles first, closest roles first, without short phrases", () => {
    const terms = searchTermsFor(profile);
    expect(terms.slice(0, 2)).toEqual(["machine learning engineer", "data scientist"]);
    // Related titles come from postings that read alike, not from a list.
    expect(terms).toContain("ml engineer");
    expect(terms).not.toContain("mle");
    expect(terms.length).toBeLessThanOrEqual(16);
    expect(searchQuery(profile).terms).toEqual(terms);
  });

  it("sends the terms and cleans descriptions from the API", async () => {
    let asked = "";
    const env = {
      fetch: async (url) => {
        asked = String(url);
        return {
          ok: true,
          json: async () => ({ title_matches: 1, jobs: [{ id: "x", title: "ML Engineer", description_text: "&lt;p&gt;PyTorch&lt;/p&gt;" }] }),
        };
      },
    };
    const jobs = await fetchCandidates(profile, env);
    expect(new URL(asked).searchParams.getAll("term")).toContain("data scientist");
    expect(jobs[0].description_text).toBe("PyTorch");
    expect(jobs.titleMatches).toBe(1);
  });

  it("says where the roles that are not shown went", () => {
    const now = Date.parse("2026-09-24T12:00:00Z");
    const fresh = "2026-09-23T00:00:00Z";
    const text = "Build ranking models in Python and PyTorch. ".repeat(4);
    const jobs = [
      { id: "1", company: "A", title: "Machine Learning Engineer", location_raw: "Remote", posted_at: fresh, description_text: text },
      { id: "2", company: "B", title: "Machine Learning Engineer", location_raw: "Berlin, Germany", posted_at: fresh, description_text: text },
      { id: "3", company: "C", title: "Machine Learning Engineer", location_raw: "Remote", posted_at: "2026-08-01T00:00:00Z", description_text: text },
      { id: "4", company: "A", title: "Machine Learning Engineer ", location_raw: "Remote", posted_at: fresh, description_text: text },
    ];
    const got = selectJobs(jobs, { ...profile, resume_text: "Machine learning engineer. Python, PyTorch, ranking." }, {}, now);
    const by = Object.fromEntries(got.breakdown.map((row) => [row.id, row.count]));
    expect(by.location).toBe(1);
    expect(by.lookback).toBe(1);
    expect(by.duplicate).toBe(1);
    expect(got.rows).toHaveLength(1);
    expect(got.breakdown.find((row) => row.id === "location").text).toBe("1 outside your places");
  });
});
