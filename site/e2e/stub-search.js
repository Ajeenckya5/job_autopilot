import { readFileSync } from "node:fs";

// The site learns which titles relate from the live feed, and the feed job rewrites that file
// every 6 hours. Browser tests read the same fixed copy the unit tests use, so they test the
// site and not whatever was posted today.
const FIXED_LEXICON = readFileSync(new URL("../test/fixtures/lexicon/lexicon.json", import.meta.url), "utf8");

export function sampleJobs() {
  const posted = new Date().toISOString();
  return [
    {
      id: "gh-ml",
      title: "Machine Learning Engineer",
      company: "Northwind",
      location_raw: "United States",
      country: "united-states",
      family: "software",
      posted_at: posted,
      url: "https://example.com/jobs/ml",
      description_text: "Python PyTorch machine learning",
      embedding: Array(384).fill(0.01),
    },
    {
      id: "gh-se",
      title: "Software Engineer",
      company: "Northwind",
      location_raw: "United States",
      country: "united-states",
      family: "software",
      posted_at: posted,
      url: "https://example.com/jobs/se",
      description_text: "Python JavaScript SQL AWS",
      embedding: Array(384).fill(0.01),
    },
    {
      id: "gh-remote",
      title: "Applied Scientist",
      company: "Northwind",
      location_raw: "Remote",
      remote_type: "remote",
      country: "remote",
      family: "general",
      posted_at: posted,
      url: "https://example.com/jobs/as",
      description_text: "Design experiments and ranking models in Python and PyTorch.",
      embedding: Array(384).fill(0.01),
    },
    {
      id: "gh-ops",
      title: "Senior DevOps Engineer",
      company: "1X",
      location_raw: "Remote",
      remote_type: "remote",
      country: "remote",
      family: "software",
      posted_at: posted,
      url: "https://example.com/jobs/ops",
      description_text: "kubernetes",
      embedding: Array(384).fill(0.01),
    },
  ];
}

export async function stubSearch(page, jobs = sampleJobs()) {
  // Only the data fetch: the bundler's own module request for the file carries a query (?url).
  await page.route((url) => /\/lexicon[^/]*\.json$/.test(url.pathname) && !url.search, (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: FIXED_LEXICON,
  }));
  // Nothing in a test reaches the live jobs API.
  await page.route("**/v1/config**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ api: true, sync: true, push: true, events: true, insights: true }),
  }));
  await page.route("**/v1/search**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ jobs: jobs.slice(0, 300) }),
  }));
  await page.route("**/v1/profile-vector**", (route) => {
    const body = route.request().postData() || "";
    if (body.includes("resume_text") || body.includes("resume_file")) {
      return route.fulfill({ status: 400, contentType: "application/json", body: "{\"ok\":false}" });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ vector: Array(384).fill(0.01) }),
    });
  });
  await page.route("**/data.usajobs.gov/**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ SearchResult: { SearchResultItems: [] } }),
  }));
}
