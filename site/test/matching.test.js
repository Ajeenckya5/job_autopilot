import { readFileSync } from "fs";
import { describe, expect, it } from "vitest";
import { rankAll } from "../src/logic/jobs.js";
import { familyChips, noteFeedback, resetFeedback } from "../src/logic/match.js";

const data = JSON.parse(readFileSync(new URL("./fixtures/matching/personas.json", import.meta.url)));
const now = Date.parse("2026-09-23T12:00:00Z");
const grade = { strong: 3, good: 2, stretch: 1, irrelevant: 0 };

function dcg(grades) {
  return grades.reduce((sum, value, index) => sum + (2 ** value - 1) / Math.log2(index + 2), 0);
}

function median(nums) {
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function profileOf(persona) {
  return {
    resume_text: persona.resume_text,
    roles: persona.roles,
    skills: persona.skills,
    locations: ["United States"],
    lookback_days: 30,
  };
}

function evaluate(persona) {
  const ranked = rankAll(persona.jobs, profileOf(persona), now);
  const labels = new Map(persona.jobs.map((job) => [job.id, job.label]));
  const top = ranked.slice(0, 10);
  const precision = top.filter((job) => labels.get(job.id) === "strong" || labels.get(job.id) === "good").length / 10;
  const actual = top.map((job) => grade[labels.get(job.id)] || 0);
  const ideal = persona.jobs.map((job) => grade[job.label]).sort((a, b) => b - a).slice(0, 10);
  const ndcg = dcg(ideal) ? dcg(actual) / dcg(ideal) : 0;
  const strongIds = persona.jobs.filter((job) => job.label === "strong").map((job) => job.id);
  const recall = ranked.slice(0, 50).filter((job) => strongIds.includes(job.id)).length / strongIds.length;
  const gap = median(ranked.filter((job) => labels.get(job.id) === "strong").map((job) => job.match_score))
    - median(ranked.filter((job) => labels.get(job.id) === "irrelevant").map((job) => job.match_score));
  return { ranked, precision, ndcg, recall, gap };
}

describe("role family matching", () => {
  it("learns related titles from postings, with no role list", () => {
    const chips = familyChips({
      roles: ["Machine Learning Engineer"],
      resume_text: "Analytics Engineer\n2018 - 2026\npython pytorch",
    });
    const labels = chips.map((chip) => chip.label);
    expect(labels).toEqual(expect.arrayContaining(["ML Engineer", "Data Engineer"]));
    chips.forEach((chip) => {
      expect(chip.kind).toBe("related");
      expect(chip.weight).toBeGreaterThan(0);
      expect(chip.weight).toBeLessThanOrEqual(0.85);
    });
    expect(familyChips({ roles: ["Sommelier"] })).toEqual([]);
  });

  it("meets precision, NDCG, recall, and score separation on 60 personas", () => {
    expect(data.personas).toHaveLength(60);
    data.personas.forEach((persona) => expect(persona.jobs).toHaveLength(50));
    const rows = data.personas.map(evaluate);
    const mean = (key) => rows.reduce((sum, row) => sum + row[key], 0) / rows.length;
    expect(mean("precision")).toBeGreaterThanOrEqual(0.9);
    expect(mean("ndcg")).toBeGreaterThanOrEqual(0.85);
    expect(mean("recall")).toBeGreaterThanOrEqual(0.9);
    expect(mean("gap")).toBeGreaterThanOrEqual(30);
  });

  it("ranks the automotive ML persona into ML, data science and the data work on the resume", () => {
    const persona = data.personas.find((row) => row.id === "ml-automotive");
    const { ranked } = evaluate(persona);
    const top = ranked.slice(0, 10).map((job) => job.title);
    expect(top.some((title) => /devops|sales/i.test(title))).toBe(false);
    // The resume also lists Analytics Engineer, whose postings read like data engineering ones.
    top.forEach((title) => {
      expect(title).toMatch(/machine learning|ml engineer|\bmle\b|ai engineer|scientist|data engineer|data platform/i);
    });
    // Scientist titles whose postings ask for as much of this resume come right after the targets.
    expect(ranked.slice(0, 10).every((job) => job.label === "strong" || job.label === "good")).toBe(true);
    expect(top.filter((title) => /machine learning|ml engineer|data scientist/i.test(title)).length).toBeGreaterThanOrEqual(6);
    const card = ranked[0];
    expect(card.tier).toBe("strong");
    expect(card.relation).toBe("Your target");
    expect(card.why_matched.length).toBeLessThanOrEqual(3);
    expect(card.why_missing.join(" ")).not.toMatch(/engineer|automotive|fixture/);
    expect(card.experience_line).toMatch(/Asks \d+\+ years, you have \d+/);
  });

  it("ranks a registered nurse into nursing roles", () => {
    const persona = data.personas.find((row) => row.id === "registered-nurse");
    const { ranked } = evaluate(persona);
    ranked.slice(0, 10).forEach((job) => {
      expect(job.title).toMatch(/nurse|rn\b|practitioner/i);
    });
    expect(ranked[0].relation).toBe("Your target");
  });

  it("hides a removed chip and resets feedback", () => {
    const persona = data.personas.find((row) => row.id === "ml-automotive");
    const profile = profileOf(persona);
    const before = rankAll(persona.jobs, profile, now);
    const chip = familyChips(profile).find((row) => row.id === "data engineer");
    const hidden = rankAll(persona.jobs, { ...profile, hidden_roles: [chip.id] }, now);
    const applied = before.find((job) => job.title === "Data Engineer");
    const after = hidden.find((job) => job.id === applied.id);
    expect(after.match_score).toBeLessThan(applied.match_score);
    const tuned = noteFeedback(profile, before[0], 1);
    expect(tuned.feedback.posCount).toBe(1);
    expect(tuned.feedback.posMean).toHaveLength(384);
    expect(JSON.stringify(tuned.feedback)).not.toContain(persona.resume_text.slice(0, 24));
    expect(resetFeedback(tuned).feedback).toBeUndefined();
  });

  it("scores 25000 candidates in under 1.5s and reranks a chip edit in under 300ms", () => {
    const embedding = Array.from({ length: 384 }, (_, index) => (index === 0 ? 1 : 0));
    const jobs = Array.from({ length: 25000 }, (_, index) => ({
      id: `j${index}`,
      title: index % 7 === 0 ? "Machine Learning Engineer" : "DevOps Engineer",
      company: "North",
      location_raw: "United States",
      posted_at: "2026-09-20T00:00:00Z",
      years_required: 4,
      skills_required: index % 7 === 0 ? ["python", "pytorch", "machine learning", "sql"] : ["docker", "kubernetes", "aws", "linux"],
      skills_preferred: [],
      embedding,
      description_text: "python pytorch machine learning",
      url: "https://example.com/j",
    }));
    const profile = profileOf(data.personas.find((row) => row.id === "ml-automotive"));
    const start = globalThis.performance.now();
    const ranked = rankAll(jobs, profile, now);
    expect(globalThis.performance.now() - start).toBeLessThan(1500);
    expect(ranked).toHaveLength(25000);
    const again = globalThis.performance.now();
    rankAll(jobs, { ...profile, hidden_roles: familyChips(profile).map((chip) => chip.id) }, now);
    expect(globalThis.performance.now() - again).toBeLessThan(300);
  });

  it("relates titles through the search's own postings when the lexicon knows too few", () => {
    const job = (id, title, company, text) => ({
      id, title, company, location_raw: "United States", posted_at: "2026-09-20T00:00:00Z", url: `https://example.com/${id}`, description_text: text,
    });
    const shop = "Run time studies, line balancing and kaizen events; value stream mapping and 5S on assembly lines.";
    const jobs = [
      ...["A", "B", "C", "D"].map((c, i) => job(`ie${i}`, "Industrial Engineer", `Plant ${c}`, shop)),
      ...["E", "F", "G"].map((c, i) => job(`me${i}`, "Manufacturing Engineer", `Works ${c}`, `Improve throughput with line balancing, kaizen events and 5S. ${shop}`)),
      ...["H", "I", "J"].map((c, i) => job(`se${i}`, "Software Engineer", `Apps ${c}`, "Build React and TypeScript front ends with GraphQL.")),
    ];
    const profile = {
      roles: ["Industrial Engineer"],
      resume_text: "Industrial Engineer\n2022 - 2026\n- Led time studies, line balancing and kaizen events\n- Value stream mapping and 5S on assembly lines",
      lookback_days: 30,
    };
    const ranked = rankAll(jobs, profile, now);
    const made = ranked.find((row) => row.title === "Manufacturing Engineer");
    const soft = ranked.find((row) => row.title === "Software Engineer");
    expect(made.relation).toBe("Related: Manufacturing Engineer");
    expect(made.bucket).toBe("match");
    expect(made.match_score).toBeGreaterThan(soft.match_score + 20);
    expect(ranked[0].relation).toBe("Your target");
  });
});

