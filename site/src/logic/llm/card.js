export function appendAiDetails(job, parent, onRescore) {
  if (!job?.ai) return;
  const line = document.createElement("p");
  line.className = "why";
  line.textContent = `Local ${Math.round(job.local_score ?? job.match_score ?? 0)}, AI ${Math.round(job.ai.score)}`;
  parent.appendChild(line);
  if (job.disagree) {
    const flag = document.createElement("p");
    flag.className = "badge";
    flag.textContent = "AI and local disagree";
    parent.appendChild(flag);
  }
  const matched = (job.ai.matched_required || []).slice(0, 3).join(", ");
  const missing = (job.ai.missing_required || []).slice(0, 3).join(", ");
  if (matched || missing) {
    const skills = document.createElement("p");
    skills.className = "why";
    const bits = [];
    if (matched) bits.push(`AI matches ${matched}`);
    if (missing) bits.push(`AI missing ${missing}`);
    skills.textContent = bits.join(". ");
    parent.appendChild(skills);
  }
  if ((job.ai.dealbreakers || []).length) {
    const blocks = document.createElement("p");
    blocks.className = "why";
    blocks.textContent = `Dealbreakers: ${job.ai.dealbreakers.slice(0, 3).join(", ")}`;
    parent.appendChild(blocks);
  }
  if (job.ai.reason) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "AI reason";
    const reason = document.createElement("p");
    reason.textContent = job.ai.reason;
    details.append(summary, reason);
    parent.appendChild(details);
  }
  const again = document.createElement("button");
  again.type = "button";
  again.className = "btn";
  again.textContent = "Rescore with AI";
  again.addEventListener("click", () => onRescore(job));
  parent.appendChild(again);
}
