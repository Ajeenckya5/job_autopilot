// Starts the daily job-autopilot run at 5:01 PM Chicago on weekdays.
//
// GitHub starts scheduled workflows hours late, so this Worker is the on-time
// starter: its Cloudflare cron fires on the minute and asks GitHub for a manual
// run. It only does that when no run has today covered already, which means a
// scheduled run that arrived early and is waiting for 5:00 PM keeps its turn,
// and a scheduled run that arrives after this one sees the manual run and stops.
//
// Secret: GH_DISPATCH_TOKEN, a fine-grained token for this repo only, with
// Actions read and write.

export const REPO = "Ajeenckya5/job_autopilot";
export const WORKFLOW = "job-autopilot.yml";
const API = "https://api.github.com";

export function chicago(date) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
    weekday: "short",
  }).formatToParts(date);
  const p = Object.fromEntries(parts.map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour), weekday: p.weekday };
}

function client(token, fetchImpl) {
  return async function gh(path, init = {}) {
    let last;
    for (let attempt = 0; attempt < 3; attempt++) {
      const res = await fetchImpl(`${API}${path}`, {
        ...init,
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "User-Agent": "job-autopilot-start",
          "X-GitHub-Api-Version": "2022-11-28",
          ...(init.body ? { "Content-Type": "application/json" } : {}),
        },
      });
      if (res.status < 500) return res;
      last = res;
      await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)));
    }
    return last;
  };
}

// Returns what it decided, as a short string, so logs and tests can read it.
export async function start(now, env, fetchImpl = fetch) {
  const today = chicago(now);
  if (today.weekday === "Sat" || today.weekday === "Sun") return "skip: weekend in Chicago";
  // The cron fires at 22:01 and 23:01 UTC; only the one that is 5 PM in Chicago counts.
  if (today.hour !== 17) return `skip: it is ${today.hour}:01 in Chicago, not 17:01`;
  if (!env.GH_DISPATCH_TOKEN) return "error: GH_DISPATCH_TOKEN is not set";

  const gh = client(env.GH_DISPATCH_TOKEN, fetchImpl);
  const list = await gh(`/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=30`);
  if (!list.ok) return `error: listing runs returned ${list.status}`;
  const runs = (await list.json()).workflow_runs || [];

  for (const run of runs) {
    if (chicago(new Date(run.created_at)).day !== today.day) continue;
    // Waiting for 5 PM, queued, or already scraping: that run has today.
    if (run.status !== "completed") return `skip: run ${run.id} (${run.event}) is ${run.status}`;
    const jobs = await gh(`/repos/${REPO}/actions/runs/${run.id}/jobs`);
    if (!jobs.ok) continue;
    const work = ((await jobs.json()).jobs || []).filter((j) => j.name.startsWith("run ("));
    if (work.length && work.every((j) => j.conclusion === "success")) {
      return `skip: run ${run.id} already sent today's files`;
    }
  }

  const res = await gh(`/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: { track: "both" } }),
  });
  if (res.status === 204) return "started: asked GitHub for today's run";
  return `error: starting the run returned ${res.status} ${await res.text()}`;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      start(new Date(event.scheduledTime), env).then((outcome) => {
        (outcome.startsWith("error") ? console.error : console.log)(outcome);
      }),
    );
  },
};
