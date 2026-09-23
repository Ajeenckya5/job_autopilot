import { readFileSync } from "fs";
import { describe, expect, it } from "vitest";
import { rankAll } from "../src/logic/jobs.js";
import { presentScore, TRUST_DEFAULT } from "../src/logic/llm/payload.js";
import { scoreJobs } from "../src/logic/llm/score.js";

const data = JSON.parse(readFileSync(new URL("./fixtures/matching/personas.json", import.meta.url)));
const now = Date.parse("2026-09-23T12:00:00Z");
const grade = { strong: 3, good: 2, stretch: 1, irrelevant: 0 };

function dcg(grades) {
  return grades.reduce((sum, value, index) => sum + (2 ** value - 1) / Math.log2(index + 2), 0);
}

function metrics(persona, ranked) {
  const labels = new Map(persona.jobs.map((job) => [job.id, job.label]));
  const top = ranked.slice(0, 10);
  const precision = top.filter((job) => labels.get(job.id) === "strong" || labels.get(job.id) === "good").length / 10;
  const actual = top.map((job) => grade[labels.get(job.id)] || 0);
  const ideal = persona.jobs.map((job) => grade[job.label]).sort((a, b) => b - a).slice(0, 10);
  const ndcg = dcg(ideal) ? dcg(actual) / dcg(ideal) : 0;
  const strongIds = persona.jobs.filter((job) => job.label === "strong").map((job) => job.id);
  const recall = ranked.slice(0, 50).filter((job) => strongIds.includes(job.id)).length / strongIds.length;
  return { precision, ndcg, recall };
}

function mean(rows) {
  return {
    precision: rows.reduce((sum, row) => sum + row.precision, 0) / rows.length,
    ndcg: rows.reduce((sum, row) => sum + row.ndcg, 0) / rows.length,
    recall: rows.reduce((sum, row) => sum + row.recall, 0) / rows.length,
  };
}

function judge(profile, job) {
  const title = String(job.title || "").toLowerCase();
  const targets = (profile.target_roles || []).map((role) => String(role).toLowerCase());
  const role = targets.some((target) => target && title.includes(target)) ? 100 : 25;
  const skills = profile.skills || [];
  const requirements = String(job.requirements || "").toLowerCase();
  const matched = skills.filter((skill) => requirements.includes(String(skill).toLowerCase()));
  const skill = skills.length ? Math.round(100 * matched.length / Math.min(skills.length, 4)) : 40;
  const score = Math.round(0.55 * role + 0.45 * Math.min(100, skill));
  const tier = score >= 85 ? "strong" : score >= 70 ? "good" : score >= 55 ? "stretch" : "hide";
  return {
    job_id: job.job_id,
    score,
    tier,
    role_fit: role,
    skills_fit: Math.min(100, skill),
    experience_fit: 80,
    matched_required: matched.slice(0, 3),
    missing_required: [],
    dealbreakers: [],
    reason: "Compared the title and the required skills.",
    confidence: 0.9,
  };
}

function judgeFetch(url, init) {
  const body = JSON.parse(init.body);
  const user = body.messages[1].content;
  const profile = JSON.parse(user.match(/Candidate profile:\n(\{[\s\S]*?\})\n/)[1]);
  const jobs = [...user.matchAll(/<untrusted_job[\s\S]*?\n(\{[\s\S]*?\})\n<\/untrusted_job>/g)].map((match) => JSON.parse(match[1]));
  const payload = { jobs: jobs.map((job) => judge(profile, job)) };
  return Promise.resolve(new Response(JSON.stringify({
    choices: [{ message: { content: JSON.stringify(payload) } }],
  }), { status: 200 }));
}

function storage() {
  const kv = new Map();
  const cache = new Map();
  return {
    get: async (key) => kv.get(key),
    set: async (key, value) => { kv.set(key, value); },
    getCache: async (key) => cache.get(key),
    setCache: async (row) => { cache.set(row.key, row); },
  };
}

async function aiById(persona, ranked) {
  const result = await scoreJobs(ranked, {
    resume_text: persona.resume_text,
    roles: persona.roles,
    skills: persona.skills,
    locations: ["United States"],
    lookback_days: 30,
  }, {
    consent: true,
    provider: "openai",
    model: "fixture",
    apiKey: "fixture",
    topN: 50,
    dailyCap: 100,
    batchSize: 8,
  }, { storage: storage(), fetchImpl: judgeFetch, now });
  return result.results;
}

describe("fixture blend", () => {
  it("compares local, AI only, and blended ranking", async () => {
    const localRows = [];
    const aiRows = [];
    const blendRows = [];
    const tops = {};
    for (const persona of data.personas) {
      const profile = {
        resume_text: persona.resume_text,
        roles: persona.roles,
        skills: persona.skills,
        locations: ["United States"],
        lookback_days: 30,
      };
      const local = rankAll(persona.jobs, profile, now);
      const ai = await aiById(persona, local);
      const withAi = local.map((job) => ({ ...job, ai: ai.get(job.id) }));
      const blended = withAi.map((job) => presentScore(job, 0.5)).sort((a, b) => b.display_score - a.display_score);
      const only = withAi.map((job) => presentScore(job, 1)).sort((a, b) => b.display_score - a.display_score);
      localRows.push(metrics(persona, local));
      aiRows.push(metrics(persona, only));
      blendRows.push(metrics(persona, blended));
      if (persona.id === "ml-automotive" || persona.id === "registered-nurse") {
        const twice = await aiById(persona, local);
        const drift = Math.max(...local.map((job) => Math.abs((ai.get(job.id)?.score || 0) - (twice.get(job.id)?.score || 0))));
        expect(drift).toBeLessThanOrEqual(5);
        tops[persona.id] = {
          local: local.slice(0, 10).map((job) => `${job.title} ${job.match_score}`),
          blend: blended.slice(0, 10).map((job) => `${job.title} ${job.display_score}`),
        };
      }
    }
    const summary = { local: mean(localRows), ai: mean(aiRows), blend: mean(blendRows), trust: TRUST_DEFAULT, tops };
    console.log(`LLM_EVAL ${JSON.stringify(summary)}`);
    const wins = summary.blend.precision >= summary.local.precision
      && summary.blend.ndcg >= summary.local.ndcg
      && summary.blend.recall >= summary.local.recall
      && (summary.blend.precision > summary.local.precision || summary.blend.ndcg > summary.local.ndcg || summary.blend.recall > summary.local.recall);
    expect(TRUST_DEFAULT).toBe(wins ? 0.5 : 0);
  }, 30000);
});
