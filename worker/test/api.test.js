import assert from "node:assert/strict";
import test from "node:test";
import { createApp } from "../src/index.js";
import { eventName, profileVectorInput, searchLimit, syncPushPayload } from "../src/jobs.js";
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

test("search returns at most 300 recent jobs in the requested family", async () => {
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
  const page = await call(app, db, "GET", "/v1/search?country=united-states&family=software&since=2026-09-01T00:00:00Z&limit=999");
  assert.equal(page.status, 200);
  assert.equal(page.json.jobs.length, 3);
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
