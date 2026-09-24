export function weekStart(iso) {
  const d = new Date(iso || Date.now());
  if (Number.isNaN(d.getTime())) return "";
  const day = d.getUTCDay();
  d.setUTCDate(d.getUTCDate() - ((day + 6) % 7));
  d.setUTCHours(0, 0, 0, 0);
  return d.toISOString().slice(0, 10);
}

export function recentWeeks(now = Date.now()) {
  const weeks = [];
  for (let i = 7; i >= 0; i -= 1) weeks.push(weekStart(new Date(now - i * 7 * 86400000).toISOString()));
  return weeks;
}

export function hiringTrend(jobs, company, now = Date.now()) {
  const weeks = recentWeeks(now);
  const wanted = String(company || "").replace(/\s+/g, " ").trim().toLowerCase();
  const counts = new Map(weeks.map((week) => [week, 0]));
  (jobs || []).forEach((job) => {
    const name = String(job.company || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (name !== wanted) return;
    const week = weekStart(job.posted_at);
    if (counts.has(week)) counts.set(week, counts.get(week) + 1);
  });
  return weeks.map((week) => ({ week, new_jobs: counts.get(week) }));
}

export function trendIndex(jobs, now = Date.now()) {
  const map = new Map();
  const names = [...new Set((jobs || []).map((job) => String(job.company || "").replace(/\s+/g, " ").trim()).filter(Boolean))];
  names.forEach((company) => map.set(company.toLowerCase(), hiringTrend(jobs, company, now)));
  return map;
}
