import { applyKit, interviewPrep } from "../premium.js";
import { positioningTips } from "./scorecard.js";

const STATUS = { met: "Met", partial: "Partial", missing: "Missing" };

export function appendAiDetails(job, parent, onRescore, resumeText = "") {
  if (!job?.ai) return;
  const ai = job.ai;
  const line = document.createElement("p");
  line.className = "why";
  const bits = [`Score ${Math.round(ai.code_score)}`];
  if (Number.isFinite(Number(ai.llm_overall))) bits.push(`model check ${Math.round(ai.llm_overall)}`);
  line.textContent = bits.join(". ");
  parent.appendChild(line);
  if (job.needsLook || ai.needs_look) {
    const flag = document.createElement("p");
    flag.className = "badge";
    flag.textContent = "Needs a look";
    parent.appendChild(flag);
  }
  if (ai.candidate_years != null && ai.years_required != null) {
    const years = document.createElement("p");
    years.className = "why";
    years.textContent = `Asks ${ai.years_required}+ years, you have ${Math.round(ai.candidate_years)}`;
    parent.appendChild(years);
  }
  const list = document.createElement("ul");
  list.className = "checklist";
  (ai.requirements || []).forEach((row) => {
    const item = document.createElement("li");
    const label = STATUS[row.status] || "Missing";
    item.textContent = `${label}. ${row.text}`;
    if (row.evidence_quote) {
      const quote = document.createElement("q");
      quote.textContent = row.evidence_quote;
      item.append(document.createTextNode(" "), quote);
    }
    list.appendChild(item);
  });
  if (list.childNodes.length) parent.appendChild(list);
  if ((ai.dealbreakers || []).length) {
    const blocks = document.createElement("p");
    blocks.className = "dealbreaker";
    blocks.textContent = ai.dealbreakers.slice(0, 4).join(". ");
    parent.appendChild(blocks);
  }
  if (ai.summary) {
    const summary = document.createElement("p");
    summary.className = "why";
    summary.textContent = ai.summary;
    parent.appendChild(summary);
  }
  const again = document.createElement("button");
  again.type = "button";
  again.className = "btn";
  again.textContent = "Rescore with AI";
  again.addEventListener("click", () => onRescore(job));
  parent.appendChild(again);
  const position = document.createElement("button");
  position.type = "button";
  position.className = "btn";
  position.textContent = "How to position your resume for this job";
  const tips = document.createElement("ul");
  tips.hidden = true;
  position.addEventListener("click", () => {
    tips.hidden = false;
    tips.replaceChildren();
    positioningTips(ai, resumeText).forEach((tip) => {
      const item = document.createElement("li");
      item.textContent = tip;
      tips.appendChild(item);
    });
  });
  parent.append(position, tips);
}

function copyButton(text) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  button.textContent = "Copy";
  button.addEventListener("click", () => {
    const value = typeof text === "function" ? text() : text;
    navigator.clipboard?.writeText(value).catch(() => {});
  });
  return button;
}

function draftBlock(title, value) {
  const wrap = document.createElement("div");
  const heading = document.createElement("p");
  heading.className = "label";
  heading.textContent = title;
  const box = document.createElement("textarea");
  box.value = value;
  wrap.append(heading, box, copyButton(() => box.value));
  return wrap;
}

export function appendApplyKit(job, parent, resumeText = "", name = "") {
  const open = document.createElement("button");
  open.type = "button";
  open.className = "btn";
  open.textContent = "Apply kit";
  const body = document.createElement("div");
  body.hidden = true;
  open.addEventListener("click", () => {
    body.hidden = false;
    body.replaceChildren();
    const kit = applyKit(job, resumeText, name);
    kit.bullets.forEach((line) => {
      const item = document.createElement("p");
      item.textContent = line;
      body.appendChild(item);
    });
    body.append(
      draftBlock("Why me", kit.why),
      draftBlock("Cover letter", kit.cover),
      draftBlock("Referral", kit.referral),
    );
  });
  parent.append(open, body);
}

export function appendInterviewPrep(job, parent, resumeText = "") {
  const open = document.createElement("button");
  open.type = "button";
  open.className = "btn";
  open.textContent = "Interview prep";
  const list = document.createElement("ul");
  list.hidden = true;
  open.addEventListener("click", () => {
    list.hidden = false;
    list.replaceChildren();
    const rows = interviewPrep(job, resumeText);
    if (!rows.length) {
      const item = document.createElement("li");
      item.textContent = "Score this job first. Questions come from its requirements.";
      list.appendChild(item);
      return;
    }
    rows.forEach((row) => {
      const item = document.createElement("li");
      item.textContent = row.story
        ? `${row.question} Use this line from the resume: “${row.story}”`
        : `${row.question} No matching line is on the resume.`;
      list.appendChild(item);
    });
  });
  parent.append(open, list);
}
