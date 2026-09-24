const HEADINGS = /^(?:benefits|perks|what we offer|compensation and benefits|equal opportunity|eeo|eeoc|diversity|legal|about (?:the )?company|company overview)\b/i;

const PARAGRAPHS = [
  /equal opportunity employer[\s\S]{0,500}/gi,
  /without regard to race[\s\S]{0,400}/gi,
  /this (?:posting|job) is not (?:a |an )?contract[\s\S]{0,240}/gi,
  /employment (?:is |with .+ )?at[- ]will[\s\S]{0,200}/gi,
  /we offer[\s\S]{0,240}?(?:insurance|401k|pto|paid time off|parental leave)[\s\S]{0,160}/gi,
];

function dropSections(text) {
  const lines = String(text || "").split(/\n/);
  const kept = [];
  let skipping = false;
  lines.forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) {
      if (!skipping) kept.push(line);
      return;
    }
    if (HEADINGS.test(trimmed)) {
      skipping = true;
      return;
    }
    if (skipping && /^(?:requirements|qualifications|responsibilities|about the role|what you(?:'|’)ll do)\b/i.test(trimmed)) {
      skipping = false;
    }
    if (!skipping) kept.push(line);
  });
  return kept.join("\n");
}

export function stripBoilerplate(text) {
  let out = dropSections(text);
  PARAGRAPHS.forEach((pattern) => {
    out = out.replace(pattern, " ");
  });
  return out.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").replace(/[ \t]{2,}/g, " ").trim();
}
