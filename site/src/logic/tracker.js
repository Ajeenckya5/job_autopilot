export const STATUSES = ["saved", "applied", "assessment", "interview", "offer", "rejected", "withdrawn"];

export function markJob(job, status, when = new Date().toISOString()) {
  const next = { ...job, status, updated_at: when };
  if (status === "applied" && !next.applied_at) next.applied_at = when;
  if (status === "interview" && !next.interview_at) next.interview_at = when;
  if (status === "rejected") next.rejected_at = when;
  if (status === "offer") next.offer_at = when;
  return next;
}

export function filterJobs(jobs, { status = "all", q = "" } = {}) {
  const query = String(q || "").trim().toLowerCase();
  return (jobs || []).filter((job) => {
    const state = job.status || "new";
    if (status && status !== "all" && state !== status) return false;
    if (!query) return true;
    const blob = `${job.title || ""} ${job.company || ""} ${job.location_raw || ""}`.toLowerCase();
    return blob.includes(query);
  });
}

export function kpis(jobs, now = Date.now()) {
  const rows = jobs || [];
  const weekAgo = now - 7 * 86400000;
  const applied = rows.filter((j) => ["applied", "assessment", "interview", "offer", "rejected"].includes(j.status));
  const appliedWeek = applied.filter((j) => new Date(j.applied_at || j.updated_at || 0).getTime() >= weekAgo);
  const responded = applied.filter((j) => ["assessment", "interview", "offer", "rejected"].includes(j.status));
  const interviews = rows.filter((j) => j.status === "interview" || j.status === "offer" || j.interview_at);
  const responseRate = applied.length ? Math.round((responded.length / applied.length) * 100) : 0;
  const interviewRate = applied.length ? Math.round((interviews.length / applied.length) * 100) : 0;
  const times = responded
    .map((j) => {
      const a = new Date(j.applied_at || 0).getTime();
      const b = new Date(j.updated_at || j.interview_at || j.rejected_at || 0).getTime();
      return a && b && b >= a ? (b - a) / 86400000 : null;
    })
    .filter((n) => n != null);
  const timeToResponse = times.length ? Math.round(times.reduce((s, n) => s + n, 0) / times.length) : null;
  return {
    applied_week: appliedWeek.length,
    applied: applied.length,
    interviews: interviews.length,
    offers: rows.filter((j) => j.status === "offer").length,
    rejected: rows.filter((j) => j.status === "rejected").length,
    response_rate: responseRate,
    interview_rate: interviewRate,
    time_to_response: timeToResponse,
  };
}

export function followUpDue(job, now = Date.now()) {
  if (job.status !== "applied" || !job.applied_at) return null;
  const age = (now - new Date(job.applied_at).getTime()) / 86400000;
  if (age >= 14) return 14;
  if (age >= 7) return 7;
  return null;
}

export function icsFor(job) {
  const start = job.interview_at ? new Date(job.interview_at) : new Date(Date.now() + 86400000);
  const stamp = start.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const endDate = new Date(start.getTime() + 3600000);
  const end = endDate.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const summary = `${job.title || "Interview"} at ${job.company || ""}`.trim();
  return [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Job Autopilot//EN",
    "BEGIN:VEVENT",
    `DTSTART:${stamp}`,
    `DTEND:${end}`,
    `SUMMARY:${summary}`,
    `URL:${job.url || ""}`,
    "END:VEVENT",
    "END:VCALENDAR",
  ].join("\r\n");
}
