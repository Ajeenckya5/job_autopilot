import SKILL_WORDS from "../data/skills.json";

export function skillsFromText(text, limit = 12) {
  const blob = ` ${String(text || "").toLowerCase()} `;
  const found = SKILL_WORDS.filter((skill) => {
    const escaped = skill.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(`[^a-z0-9+]${escaped}[^a-z0-9+]`).test(blob);
  });
  return limit == null ? found : found.slice(0, limit);
}

export function allSkillsIn(text) {
  return skillsFromText(text, null);
}

export function suggestTitles(text) {
  const skills = skillsFromText(text);
  const blob = String(text || "").toLowerCase();
  const titles = [];
  if (/machine learning|pytorch|tensorflow|nlp/.test(blob)) titles.push("Machine learning engineer");
  if (/nurse|nursing|patient/.test(blob)) titles.push("Registered nurse");
  if (/teacher|classroom|curriculum/.test(blob)) titles.push("Teacher");
  if (/warehouse|forklift|logistics/.test(blob)) titles.push("Logistics coordinator");
  if (/account|ledger|gaap/.test(blob)) titles.push("Accountant");
  if (/javascript|react|typescript/.test(blob)) titles.push("Software engineer");
  if (!titles.length && skills.length) titles.push("Software engineer");
  return titles.slice(0, 3);
}

export function guessName(text) {
  const line = String(text || "").split(/\n/).map((s) => s.trim()).find((s) => s.length > 3 && s.length < 60);
  if (!line) return "";
  if (/resume|curriculum|objective|summary|experience/i.test(line)) return "";
  const parts = line.split(/\s+/);
  if (parts.length < 2 || parts.length > 4) return "";
  if (!parts.every((p) => /^[A-Z][a-z'.-]+$/.test(p))) return "";
  return line;
}

export async function readResumeFile(file, deps = {}) {
  if (!file) throw new Error("Choose a resume file.");
  const name = file.name || "resume";
  const lower = name.toLowerCase();
  if (lower.endsWith(".doc") && !lower.endsWith(".docx")) {
    throw new Error("This .doc file can’t be read. Save it as PDF or DOCX and try again.");
  }
  if (lower.endsWith(".docx")) {
    const mammoth = deps.mammoth;
    if (!mammoth) throw new Error("DOCX reader failed to load. Try a PDF.");
    const raw = await file.arrayBuffer();
    const bytes = raw instanceof Uint8Array ? raw : new Uint8Array(raw);
    const arrayBuffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    const result = await mammoth.extractRawText({ buffer: bytes, arrayBuffer });
    const text = String(result.value || "").replace(/\s+/g, " ").trim();
    if (text.length < 40) throw new Error("That DOCX had almost no text. Try a PDF export.");
    return { name, text, skills: skillsFromText(text) };
  }
  if (lower.endsWith(".pdf") || file.type === "application/pdf") {
    const buf = await file.arrayBuffer();
    const pdfjs = deps.pdfjs;
    if (!pdfjs) throw new Error("PDF reader failed to load. Refresh and try again.");
    const doc = await pdfjs.getDocument({ data: buf }).promise;
    const pages = Math.min(doc.numPages || 0, 6);
    const chunks = [];
    for (let i = 1; i <= pages; i += 1) {
      const page = await doc.getPage(i);
      const content = await page.getTextContent();
      chunks.push(content.items.map((item) => item.str).join(" "));
    }
    const text = chunks.join(" ").replace(/\s+/g, " ").trim();
    if (text.length < 40) {
      throw new Error("This PDF has no text layer. Export a text PDF, not a scan.");
    }
    return { name, text, skills: skillsFromText(text) };
  }
  const text = String(await file.text()).replace(/\s+/g, " ").trim();
  if (text.length < 40) throw new Error("That file has too little text to score against.");
  return { name, text, skills: skillsFromText(text) };
}
