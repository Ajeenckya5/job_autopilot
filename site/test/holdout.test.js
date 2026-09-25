import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { scoreAll } from "../src/logic/match.js";

const data = JSON.parse(readFileSync(new URL("./fixtures/holdout/holdout.json", import.meta.url), "utf8"));
const NOW = Date.parse("2026-09-24T12:00:00Z");

function auc(pos, neg) {
  if (!pos.length || !neg.length) return 1;
  let wins = 0;
  pos.forEach((p) => neg.forEach((n) => {
    wins += p > n ? 1 : p === n ? 0.5 : 0;
  }));
  return wins / (pos.length * neg.length);
}

export function holdoutMetrics() {
  const rows = Object.entries(data.personas).map(([id, persona]) => {
    const jobs = data.jobs.filter((job) => job.persona === id);
    const truth = new Map(jobs.map((job) => [job.id, job.relevant]));
    const ranked = scoreAll(jobs, { ...persona, lookback_days: 30 }, NOW);
    const top = ranked.slice(0, 10);
    const relevant = jobs.filter((job) => job.relevant).length;
    const shown = ranked.filter((job) => job.bucket === "match");
    return {
      id,
      relevant,
      precisionAt10: top.filter((job) => truth.get(job.id)).length / Math.min(10, top.length),
      recallShown: shown.filter((job) => truth.get(job.id)).length / relevant,
      precisionShown: shown.length ? shown.filter((job) => truth.get(job.id)).length / shown.length : 0,
      auc: auc(ranked.filter((job) => truth.get(job.id)).map((job) => job.match_score), ranked.filter((job) => !truth.get(job.id)).map((job) => job.match_score)),
    };
  });
  const mean = (key) => rows.reduce((sum, row) => sum + row[key], 0) / rows.length;
  return { rows, precisionAt10: mean("precisionAt10"), recallShown: mean("recallShown"), precisionShown: mean("precisionShown"), auc: mean("auc") };
}

describe("independent holdout", () => {
  it("has 330 labelled real postings across six people", () => {
    expect(data.jobs).toHaveLength(330);
    expect(data.jobs.filter((job) => job.relevant)).toHaveLength(72);
  });

  // Floors sit just under what the scorer measured. Sep 2026 with fixed role lists: precision@10
  // 0.67, recall 0.65, precision of shown 0.61, AUC 0.92. With skills and titles learned from the
  // postings: 0.75, 0.68, 0.82, 0.93. Raise them as it improves.
  it("keeps honest floors on real postings", () => {
    const got = holdoutMetrics();
    console.log(`HOLDOUT ${JSON.stringify(got, (key, value) => (typeof value === "number" ? Math.round(value * 100) / 100 : value))}`);
    expect(got.precisionAt10).toBeGreaterThanOrEqual(0.72);
    expect(got.recallShown).toBeGreaterThanOrEqual(0.64);
    expect(got.precisionShown).toBeGreaterThanOrEqual(0.78);
    expect(got.auc).toBeGreaterThanOrEqual(0.91);
    got.rows.forEach((row) => expect(row.auc, row.id).toBeGreaterThanOrEqual(0.72));
  });
});
