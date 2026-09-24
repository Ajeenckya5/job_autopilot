import { describe, expect, it } from "vitest";
import { applyKit, followUpDraft, interviewPrep } from "../src/logic/premium.js";

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

  it("writes a follow-up without new experience", () => {
    const note = followUpDraft(job, 7);
    expect(note).toContain("Machine Learning Engineer");
    expect(note).toContain("Northwind");
    expect(note).toContain("7 days");
    expect(note).not.toMatch(/pytorch|spark/i);
  });
});
