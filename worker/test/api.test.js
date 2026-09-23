import assert from "node:assert/strict";
import test from "node:test";
import { createApp } from "../src/index.js";
import { eventName, syncPushPayload } from "../src/jobs.js";
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

test("event bodies cannot carry resume text", async () => {
  const resume = "UNIQUE_RESUME_SENTINEL machine learning engineer with pytorch and a long private history";
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
