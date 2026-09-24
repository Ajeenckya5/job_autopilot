import { describe, expect, it } from "vitest";
import { applyKit, followUpDraft, interviewPrep } from "../src/logic/premium.js";

function quotedSpans(text) {
  return [...String(text).matchAll(/[“"]([^”"]+)[”"]/g)].map((match) => match[1]);
}

function normalizeSpace(text) {
  return String(text || "").toLowerCase().replace(/\s+/g, " ").trim();
}

const resume = "Machine learning engineer. Python, PyTorch, and SQL. Shipped ranking models.";
const job = {
  title: "Machine Learning Engineer",
  company: "Northwind",
  applied_at: "2026-09-01",
  ai: {
    requirements: [
      { text: "Python", status: "met", evidence_quote: "Python, PyTorch, and SQL" },
      { text: "Spark", status: "missing", evidence_quote: "ten years of Spark" },
    ],
  },
};

describe("apply kit", () => {
  it("uses only resume lines that were verified", () => {
    const kit = applyKit(job, resume, "Ada");
    expect(kit.cover).toContain("Python, PyTorch, and SQL");
    expect(kit.cover).not.toContain("Spark");
    expect(kit.cover).not.toContain("ten years");
    expect(kit.referral).toContain("Python, PyTorch, and SQL");
    expect(kit.bullets).toHaveLength(1);
  });

  it("pairs interview questions with a resume story or nothing", () => {
    const prep = interviewPrep(job, resume);
    expect(prep[0].story).toBe("Python, PyTorch, and SQL");
    expect(prep[1].story).toBe("");
  });

  it("quotes only lines that appear in the resume", () => {
    const kit = applyKit(job, resume, "Ada");
    const prep = interviewPrep(job, resume);
    const spans = [kit.cover, kit.referral, kit.why, ...kit.bullets].flatMap(quotedSpans);
    expect(spans.length).toBeGreaterThan(0);
    const resumeText = normalizeSpace(resume);
    spans.forEach((span) => expect(resumeText).toContain(normalizeSpace(span)));
    prep.forEach((row) => {
      if (row.story) expect(resumeText).toContain(normalizeSpace(row.story));
    });
    const dumped = `${kit.cover}\n${kit.referral}\n${kit.why}\n${kit.bullets.join("\n")}\n${prep.map((row) => row.story).join("\n")}`;
    expect(dumped).not.toContain("ten years of Spark");
  });

  it("writes a follow-up without new experience", () => {
    const note = followUpDraft(job, 7);
    expect(note).toContain("Machine Learning Engineer");
    expect(note).toContain("Northwind");
    expect(note).toContain("7 days");
    expect(note).not.toMatch(/pytorch|spark/i);
  });
});
