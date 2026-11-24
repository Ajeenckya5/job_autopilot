import { test } from "node:test";
import assert from "node:assert/strict";
import { start, chicago } from "../src/start.js";

const env = { GH_DISPATCH_TOKEN: "test-token" };

// A fake GitHub API: runs is the workflow's run list, jobs maps run id to its jobs.
function fakeGitHub({ runs = [], jobs = {}, dispatchStatus = 204, failFirst = 0 } = {}) {
  const calls = [];
  let failures = failFirst;
  const impl = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", body: init.body, auth: init.headers.Authorization });
    if (failures > 0) { failures--; return new Response("busy", { status: 502 }); }
    if (url.endsWith("/dispatches")) return new Response(null, { status: dispatchStatus });
    const m = url.match(/\/runs\/(\d+)\/jobs$/);
    if (m) return Response.json({ jobs: jobs[m[1]] || [] });
    return Response.json({ workflow_runs: runs });
  };
  return { impl, calls };
}

const at = (iso) => new Date(iso);
const ok = (names) => names.map((name) => ({ name, conclusion: "success" }));

test("chicago() follows daylight saving", () => {
  assert.equal(chicago(at("2026-11-30T23:01:00Z")).hour, 17); // CST
  assert.equal(chicago(at("2027-06-15T22:01:00Z")).hour, 17); // CDT
  assert.equal(chicago(at("2026-11-24T03:24:00Z")).day, "2026-11-23");
});

test("starts the run at 5:01 PM CST when nothing covers today", async () => {
  const gh = fakeGitHub({ runs: [{ id: 1, created_at: "2026-11-30T07:30:00Z", status: "completed", event: "schedule" }], jobs: { 1: [{ name: "gate", conclusion: "success" }, { name: "run (ai)", conclusion: "skipped" }, { name: "run (ie)", conclusion: "skipped" }] } });
  const out = await start(at("2026-11-30T23:01:00Z"), env, gh.impl);
  assert.equal(out, "started: asked GitHub for today's run");
  const post = gh.calls.find((c) => c.method === "POST");
  assert.match(post.url, /Ajeenckya5\/job_autopilot\/actions\/workflows\/job-autopilot\.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(post.body), { ref: "main", inputs: { track: "both" } });
  assert.equal(post.auth, "Bearer test-token");
});

test("starts in summer from the 22:01 UTC slot and skips the 23:01 one", async () => {
  assert.equal(await start(at("2027-06-15T22:01:00Z"), env, fakeGitHub().impl), "started: asked GitHub for today's run");
  assert.match(await start(at("2027-06-15T23:01:00Z"), env, fakeGitHub().impl), /^skip: it is 18:01/);
});

test("skips the 22:01 UTC slot in winter (4:01 PM CST)", async () => {
  const gh = fakeGitHub();
  assert.match(await start(at("2026-11-30T22:01:00Z"), env, gh.impl), /^skip: it is 16:01/);
  assert.equal(gh.calls.length, 0);
});

test("leaves today to a scheduled run that is waiting for 5 PM", async () => {
  const gh = fakeGitHub({ runs: [{ id: 7, created_at: "2026-11-30T18:40:00Z", status: "in_progress", event: "schedule" }] });
  assert.match(await start(at("2026-11-30T23:01:00Z"), env, gh.impl), /^skip: run 7 \(schedule\) is in_progress/);
  assert.ok(!gh.calls.some((c) => c.method === "POST"));
});

test("does not send twice when a run already sent today's files", async () => {
  const gh = fakeGitHub({ runs: [{ id: 9, created_at: "2026-11-30T15:00:00Z", status: "completed", event: "workflow_dispatch" }], jobs: { 9: ok(["run (ai)", "run (ie)"]) } });
  assert.match(await start(at("2026-11-30T23:01:00Z"), env, gh.impl), /already sent/);
  assert.ok(!gh.calls.some((c) => c.method === "POST"));
});

test("ignores yesterday's runs", async () => {
  const gh = fakeGitHub({ runs: [{ id: 3, created_at: "2026-11-30T03:24:00Z", status: "in_progress", event: "schedule" }] }); // Nov 29, 9:24 PM Chicago
  assert.equal(await start(at("2026-11-30T23:01:00Z"), env, gh.impl), "started: asked GitHub for today's run");
});

test("reruns when today's earlier run failed", async () => {
  const gh = fakeGitHub({ runs: [{ id: 4, created_at: "2026-11-30T17:00:00Z", status: "completed", event: "workflow_dispatch" }], jobs: { 4: [{ name: "run (ai)", conclusion: "success" }, { name: "run (ie)", conclusion: "failure" }] } });
  assert.equal(await start(at("2026-11-30T23:01:00Z"), env, gh.impl), "started: asked GitHub for today's run");
});

test("does nothing on weekends", async () => {
  const gh = fakeGitHub();
  assert.equal(await start(at("2026-12-05T23:01:00Z"), env, gh.impl), "skip: weekend in Chicago");
  assert.equal(gh.calls.length, 0);
});

test("retries when GitHub has a server error", async () => {
  const gh = fakeGitHub({ failFirst: 1 });
  assert.equal(await start(at("2026-11-30T23:01:00Z"), env, gh.impl), "started: asked GitHub for today's run");
});

test("reports a missing token or a refused dispatch", async () => {
  assert.equal(await start(at("2026-11-30T23:01:00Z"), {}, fakeGitHub().impl), "error: GH_DISPATCH_TOKEN is not set");
  assert.match(await start(at("2026-11-30T23:01:00Z"), env, fakeGitHub({ dispatchStatus: 403 }).impl), /^error: starting the run returned 403/);
});
