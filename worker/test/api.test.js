import assert from "node:assert/strict";
import test from "node:test";
import { createApp } from "../src/index.js";
import { readFileSync } from "node:fs";
import { countryOf, eventName, profileVectorInput, searchJobs, searchLimit, searchTerms, syncPushPayload } from "../src/jobs.js";
import { memoryKv, openLocalD1 } from "../src/sqlite-d1.js";

function env() {
  return { DB: openLocalD1(), CONFIG: memoryKv(), INGEST_TOKEN: "test-token" };
}

async function call(app, environment, method, path, body, headers = {}) {
  const response = await app.fetch(new Request(`http://127.0.0.1${path}`, {
    method,
    headers: { "content-type": "application/json", ...headers },
    body: body == null ? undefined : (typeof body === "string" ? body : JSON.stringify(body)),
  }), environment);
  const text = await response.text();
  return { status: response.status, json: text ? JSON.parse(text) : null, text };
}

test("config returns the public push key only", async () => {
  const app = createApp();
  const db = env();
  db.VAPID_PUBLIC_KEY = "public-key";
  db.VAPID_PRIVATE_KEY = "private-key";
  const config = await call(app, db, "GET", "/v1/config");
  assert.equal(config.json.vapid_public_key, "public-key");
  assert.equal(JSON.stringify(config.json).includes("private-key"), false);
});

test("deltas return only jobs changed after the cursor", async () => {
  const app = createApp();
  const db = env();
  const first = await call(app, db, "POST", "/v1/jobs", {
    jobs: [{
      id: "gh-1",
      source: "greenhouse",
      company: "Northwind",
      title: "Machine Learning Engineer",
      url: "https://example.com/jobs/1",
      location_raw: "United States",
      posted_at: "2026-09-23T12:00:00Z",
      description_text: "Python models",
    }],
  }, { "x-ingest-token": "test-token" });
  assert.equal(first.status, 200);
  const page = await call(app, db, "GET", "/v1/jobs?since=0");
  assert.equal(page.json.jobs.length, 1);
  assert.equal(page.json.jobs[0].title, "Machine Learning Engineer");
  const again = await call(app, db, "GET", `/v1/jobs?since=${page.json.cursor}`);
  assert.equal(again.json.jobs.length, 0);
});

test("search returns at most 300 recent jobs in any field", async () => {
  const app = createApp();
  const db = env();
  const jobs = [];
  for (let i = 0; i < 5; i += 1) {
    jobs.push({
      id: `gh-${i}`,
      source: "greenhouse",
      company: "Northwind",
      title: i === 4 ? "Registered Nurse" : "Software Engineer",
      url: `https://example.com/jobs/${i}`,
      location_raw: "United States",
      posted_at: i === 1 ? "2020-01-01T00:00:00Z" : "2026-09-20T00:00:00Z",
      description_text: "Python",
    });
  }
  const saved = await call(app, db, "POST", "/v1/jobs", { jobs }, { "x-ingest-token": "test-token" });
  assert.equal(saved.status, 200);
  // An old site still sends a family; it no longer narrows anything.
  const page = await call(app, db, "GET", "/v1/search?country=united-states&family=software&since=2026-09-01T00:00:00Z&limit=999");
  assert.equal(page.status, 200);
  assert.equal(page.json.jobs.length, 4);
  assert.equal(page.json.jobs.some((job) => job.title === "Registered Nurse"), true);
  assert.equal(page.json.jobs.every((job) => job.embedding.length === 384), true);
  assert.equal(searchLimit(999), 300);
  assert.equal(searchLimit(0), 1);
});

test("profile vector accepts skills and roles only", async () => {
  const resume = "UNIQUE_RESUME_SENTINEL";
  assert.equal(profileVectorInput({ skills: ["python"], roles: ["Software Engineer"], resume_text: resume }).error, "invalid");
  const app = createApp();
  const db = env();
  const bad = await call(app, db, "POST", "/v1/profile-vector", { skills: ["python"], roles: ["Software Engineer"], resume_text: resume });
  assert.equal(bad.status, 400);
  assert.equal(bad.text.includes(resume), false);
  const good = await call(app, db, "POST", "/v1/profile-vector", { skills: ["python"], roles: ["Software Engineer"] });
  assert.equal(good.status, 200);
  assert.equal(good.json.vector.length, 384);
  assert.equal(JSON.stringify(good.json).includes(resume), false);
});

test("a job description is available on demand without the vector", async () => {
  const app = createApp();
  const db = env();
  await call(app, db, "POST", "/v1/jobs", {
    jobs: [{
      id: "gh-desc",
      source: "greenhouse",
      company: "Northwind",
      title: "Software Engineer",
      url: "https://example.com/jobs/desc",
      location_raw: "United States",
      posted_at: "2026-09-20T00:00:00Z",
      description_text: "Must have python.\nBenefits\nWe offer paid time off.",
    }],
  }, { "x-ingest-token": "test-token" });
  const job = await call(app, db, "GET", "/v1/job/gh-desc");
  assert.equal(job.status, 200);
  assert.equal(job.json.description_text.includes("Must have python."), true);
  assert.equal(job.json.embedding, undefined);
});

test("event bodies cannot carry resume text", async () => {
  const resume = "UNIQUE_RESUME_SENTINEL";
  assert.equal(eventName(JSON.stringify({ name: "search_run" })).name, "search_run");
  assert.equal(eventName(JSON.stringify({ name: "search_run", resume_text: resume })).error, "invalid");
  assert.equal(syncPushPayload(), "{\"type\":\"sync\"}");
  assert.equal(syncPushPayload().includes(resume), false);

  const app = createApp();
  const db = env();
  const bad = await call(app, db, "POST", "/v1/events", JSON.stringify({ name: "search_run", resume_text: resume }));
  assert.equal(bad.status, 400);
  const good = await call(app, db, "POST", "/v1/events", JSON.stringify({ name: "search_run" }));
  assert.equal(good.status, 200);
  const funnel = await call(app, db, "GET", "/v1/funnel");
  assert.equal(funnel.json.weeks.length, 1);
  assert.equal(funnel.json.weeks[0].name, "search_run");
  assert.equal(JSON.stringify(funnel.json).includes(resume), false);
});

test("a US city with a state code is the United States", () => {
  assert.equal(countryOf("San Francisco, CA"), "united-states");
  assert.equal(countryOf("Los Angeles, CA (On-site)"), "united-states");
  assert.equal(countryOf("Austin,TX"), "united-states");
  assert.equal(countryOf("Toronto, ON"), "other");
  assert.equal(countryOf("Vancouver, Canada"), "other");
  assert.equal(countryOf("Remote"), "remote");
  assert.equal(countryOf("", [{ country: "United States" }]), "united-states");
  assert.equal(countryOf("Vancouver, CA", [{ country: "Canada" }]), "other");
});

test("the relabel migration moves US rows out of other and leaves the rest", async () => {
  const db = openLocalD1();
  const rows = [
    ["a", "San Francisco, CA ", []],
    ["b", "Toronto, ON", []],
    ["c", "", [{ country: "United States" }]],
    ["d", "Vancouver, CA", [{ country: "Canada" }]],
    ["e", "Cambridge, MA; London, United Kingdom", []],
  ];
  for (const [id, location_raw, locations] of rows) {
    await db.prepare(
      "INSERT INTO jobs (id, country, family, company, updated_at, posted_at, cursor, content_hash, payload) VALUES (?, 'other', 'software', 'x', '', '', 1, 'h', ?)",
    ).bind(id, JSON.stringify({ location_raw, locations })).run();
  }
  const sql = readFileSync(new URL("../migrations/0002_us_state_codes.sql", import.meta.url), "utf8");
  await db.prepare(sql.split("\n").filter((line) => !line.startsWith("--")).join("\n")).bind().run();
  const got = (await db.prepare("SELECT id, country FROM jobs ORDER BY id").bind().all()).results;
  assert.deepEqual(got.map((row) => row.country), ["united-states", "other", "united-states", "other", "other"]);
});

test("search returns titles that fit the roles across the look-back before newer others", async () => {
  const app = createApp();
  const db = env();
  const jobs = [];
  for (let i = 0; i < 6; i += 1) {
    jobs.push({
      id: `gh-new-${i}`,
      source: "greenhouse",
      company: "Northwind",
      title: "Product Designer",
      url: `https://example.com/jobs/new-${i}`,
      location_raw: "Seattle, WA",
      posted_at: `2026-09-2${i % 4}T00:00:00Z`,
      description_text: "&lt;p&gt;Figma &amp;amp; research&lt;/p&gt;",
    });
  }
  jobs.push({
    id: "gh-ml",
    source: "greenhouse",
    company: "Northwind",
    title: "Senior Machine Learning Engineer",
    url: "https://example.com/jobs/ml",
    location_raw: "San Francisco, CA",
    posted_at: "2026-09-05T00:00:00Z",
    description_text: "<p>PyTorch</p>",
  });
  await call(app, db, "POST", "/v1/jobs", { jobs }, { "x-ingest-token": "test-token" });
  const page = await call(app, db, "GET", "/v1/search?country=united-states&family=software&since=2026-09-01T00:00:00Z&limit=3&term=machine%20learning&term=ml");
  assert.equal(page.json.jobs[0].id, "gh-ml");
  assert.equal(page.json.jobs.length, 3);
  assert.equal(page.json.title_matches, 1);
  assert.equal(page.json.jobs[1].description_text, "Figma & research");
  // Whole-word matching makes two-letter titles safe to search.
  assert.deepEqual(searchTerms(["ML", "Machine  Learning", "100%_sure", "x".repeat(50)]), ["ml", "machine learning", "100 sure"]);
});


test("a batch of jobs gets ordered cursors in one transaction and unchanged jobs are skipped", async () => {
  const app = createApp();
  const db = env();
  const jobs = Array.from({ length: 25 }, (_, i) => ({
    id: `gh-batch-${i}`,
    source: "greenhouse",
    company: `Company ${i % 3}`,
    title: "Data Scientist",
    url: `https://example.com/jobs/batch-${i}`,
    location_raw: "Boston, MA",
    posted_at: "2026-09-20T00:00:00Z",
    description_text: "&lt;p&gt;SQL&lt;/p&gt;",
  }));
  const first = await call(app, db, "POST", "/v1/jobs", { jobs }, { "x-ingest-token": "test-token" });
  assert.equal(first.json.changed, 25);
  assert.equal(first.json.cursor, "25");
  const again = await call(app, db, "POST", "/v1/jobs", { jobs }, { "x-ingest-token": "test-token" });
  assert.equal(again.json.changed, 0);
  const edited = await call(app, db, "POST", "/v1/jobs", {
    jobs: [{ ...jobs[3], title: "Senior Data Scientist" }, jobs[4]],
  }, { "x-ingest-token": "test-token" });
  assert.equal(edited.json.changed, 1);
  assert.equal(edited.json.cursor, "26");
  const page = await call(app, db, "GET", "/v1/jobs?since=0");
  const cursors = new Map(page.json.jobs.map((job, index) => [job.id, index]));
  assert.equal(page.json.jobs.length, 25);
  assert.equal(page.json.jobs.at(-1).id, "gh-batch-3");
  assert.equal(cursors.get("gh-batch-0") < cursors.get("gh-batch-24"), true);
  assert.equal(page.json.jobs[0].country, "united-states");
  assert.equal(page.json.jobs[0].description_text, "SQL");
});

test("after titles, postings that use the resume's skill phrases come first, hyphens or not", async () => {
  const app = createApp();
  const db = env();
  const base = { source: "greenhouse", company: "Harper LLP", location_raw: "Chicago, IL" };
  await call(app, db, "POST", "/v1/jobs", {
    jobs: [
      { ...base, id: "a", title: "Office Coordinator", url: "https://example.com/a", posted_at: "2026-09-22T00:00:00Z", description_text: "Order supplies and greet visitors." },
      { ...base, id: "b", title: "Litigation Support Specialist", url: "https://example.com/b", posted_at: "2026-09-10T00:00:00Z", description_text: "Draft pleadings and run e-Discovery reviews." },
      { ...base, id: "c", title: "Case Clerk", url: "https://example.com/c", posted_at: "2026-09-15T00:00:00Z", description_text: "File pleadings with the court." },
      { ...base, id: "d", title: "Paralegal", url: "https://example.com/d", posted_at: "2026-09-02T00:00:00Z", description_text: "Support attorneys." },
    ],
  }, { "x-ingest-token": "test-token" });
  const page = await call(app, db, "GET", "/v1/search?country=united-states&since=2026-09-01T00:00:00Z&term=paralegal&skill=pleadings&skill=ediscovery");
  assert.deepEqual(page.json.jobs.map((job) => job.id), ["d", "b", "c", "a"]);
  assert.equal(page.json.title_matches, 1);
});

test("the search columns migration fills titles and descriptions for rows already stored", async () => {
  const db = openLocalD1();
  await db.prepare(
    "INSERT INTO jobs (id, country, family, company, updated_at, posted_at, cursor, content_hash, payload) VALUES ('x', 'united-states', '', 'x', '', '', 1, 'h', ?)",
  ).bind(JSON.stringify({ title: "E-Discovery Paralegal", description_text: "Run e-discovery in Relativity/Everlaw." })).run();
  const sql = readFileSync(new URL("../migrations/0003_search_any_field.sql", import.meta.url), "utf8");
  const update = sql.slice(sql.indexOf("UPDATE jobs"), sql.indexOf("CREATE INDEX"));
  await db.prepare(update.trim().replace(/;$/, "")).bind().run();
  const row = await db.prepare("SELECT title_lc, text_lc FROM jobs WHERE id = 'x'").bind().first();
  // Close enough until the next full feed rewrites the row through searchable().
  assert.equal(row.title_lc, "e discovery paralegal");
  assert.equal(row.text_lc, "run e discovery in relativity everlaw.");
  const page = await searchJobs(db, { countries: ["united-states"], since: "", skills: ["relativity"], terms: [] });
  assert.equal(page.jobs.length, 0, "rows without a posting date stay out of the look-back");
});

test("search matches whole words, so a short skill does not find longer words", async () => {
  const app = createApp();
  const db = env();
  const base = { source: "greenhouse", company: "Plant Co", location_raw: "Columbus, OH", posted_at: "2026-09-20T00:00:00Z" };
  await call(app, db, "POST", "/v1/jobs", {
    jobs: [
      { ...base, id: "clean", title: "Cleanroom Technician", url: "https://example.com/clean", description_text: "Keep the cleanroom clean." },
      { ...base, id: "lean", title: "Process Engineer", url: "https://example.com/lean", description_text: "Run Lean, 5S and kaizen." },
      { ...base, id: "rn", title: "ICU Nurse (RN)", url: "https://example.com/rn", description_text: "Critical care." },
    ],
  }, { "x-ingest-token": "test-token" });
  const lean = await call(app, db, "GET", "/v1/search?country=united-states&since=2026-09-01T00:00:00Z&skill=lean&limit=1");
  assert.equal(lean.json.jobs[0].id, "lean");
  const nurse = await call(app, db, "GET", "/v1/search?country=united-states&since=2026-09-01T00:00:00Z&term=rn&limit=1");
  assert.equal(nurse.json.jobs[0].id, "rn");
  assert.equal(nurse.json.title_matches, 1);
});
