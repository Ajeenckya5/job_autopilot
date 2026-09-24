import { describe, expect, it } from "vitest";
import { stripBoilerplate } from "../src/logic/llm/boilerplate.js";
import { codeScore, compactResult, dealbreakerConflicts, quoteFound, verifyRequirements } from "../src/logic/llm/scorecard.js";

const resume = "Machine Learning Engineer\n2018 - 2026\nPython and PyTorch for vehicle models.";

describe("job description boilerplate", () => {
  it("drops benefits, perks, and legal text, and can keep the posting verbatim", () => {
    const posted = [
      "About the role",
      "Must have python.",
      "Benefits",
      "We offer paid time off, a retirement plan, and parental leave.",
      "Equal opportunity",
      "We are an equal opportunity employer.",
      "Legal",
      "This posting is not a contract of employment.",
    ].join("\n");
    const stripped = stripBoilerplate(posted);
    expect(stripped).toContain("Must have python.");
    expect(stripped).not.toMatch(/equal opportunity|retirement plan|not a contract/i);
    expect(posted).toContain("equal opportunity");
  });
});

describe("evidence and the code score", () => {
  it("downgrades a claim whose quote is not on the resume", () => {
    const verified = verifyRequirements([
      { text: "python", type: "must", status: "met", evidence_quote: "Python" },
      { text: "kubernetes", type: "must", status: "met", evidence_quote: "invented kubernetes career" },
      { text: "cooking", type: "nice", status: "partial", evidence_quote: "not on the resume" },
    ], resume);
    expect(verified.requirements[0].status).toBe("met");
    expect(verified.requirements[1].status).toBe("partial");
    expect(verified.requirements[2].status).toBe("missing");
    expect(verified.rate).toBeCloseTo(1 / 3);
    expect(quoteFound(resume, "Python and PyTorch")).toBe(true);
  });

  it("caps a sponsorship conflict at 40 and stores a compact result", () => {
    const result = {
      requirements: [
        { text: "python", type: "must", status: "met", evidence_quote: "Python" },
      ],
      years_required: 6,
      seniority_fit: "match",
      role_fit: 1,
      dealbreakers: ["no visa sponsorship"],
      llm_overall: 95,
    };
    const open = codeScore({ ...result, dealbreakers: [] }, { resume_text: resume }, resume);
    const capped = codeScore(result, { resume_text: resume, sponsorship_needed: true }, resume);
    expect(open).toBeGreaterThan(40);
    expect(capped).toBe(40);
    expect(dealbreakerConflicts(["no visa sponsorship"], { sponsorship_needed: false })).toBe(false);
    const stored = compactResult({ ...result, job_id: "ml", code_score: capped, summary: "word ".repeat(80) });
    expect(JSON.stringify(stored).length).toBeLessThanOrEqual(2048);
  });
});
