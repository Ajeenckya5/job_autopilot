import { migrateLegacyJobs, openStore, writeSavedJobs } from "./db.js";
import { jobsToXlsx } from "./logic/excel.js";
import { cleanCompany, collapsePostings, rankAll, searchPool, selectJobs } from "./logic/jobs.js";
import { apiBase, pullConfig } from "./logic/sync.js";
import { familyChips, noteFeedback, resetFeedback } from "./logic/match.js";
import { absorbResume, profileFromResume } from "./logic/profile.js";
import { fetchCandidates, fetchJobText, fetchProfileVector } from "./logic/search.js";
import { appendAiDetails, appendApplyKit, appendInterviewPrep } from "./logic/llm/card.js";
import { macSaveKey, migrateMacKeys, openSecret, sealSecret } from "./logic/llm/keys.js";
import { buildPrompt, DAILY_CAP_DEFAULT, estimateTokens, presentScore, TOP_N_DEFAULT } from "./logic/llm/payload.js";
import { isMacHost, providersForMode } from "./logic/llm/providers.js";
import { listModels, scoreJobs } from "./logic/llm/score.js";
import { clearAppData, enforceStorageBudget, formatMegabytes, measureStorage } from "./logic/storage.js";
import { guessName, readResumeFile, skillsFromText, suggestTitles } from "./logic/resume.js";
import { loadSentry } from "./sentry.js";
import { followUpDue, icsFor, kpis, markJob, STATUSES } from "./logic/tracker.js";
import { PLAN, SHORTCUTS, WHATS_NEW, followUpDraft } from "./logic/premium.js";
import { parseRunsPerDay, plural, safeHref } from "./logic/text.js";
import { mergeSources } from "./lib/sources.js";

const KEY = "jobAutopilotSetup";
const JOBS = "jobAutopilotJobs";
const DRAFT = "jobAutopilotDraft";
let dragId = "";
let jobCache = [];
let configCache = { sync: true, push: true, events: true, insights: true, api: true };
let persistChain = Promise.resolve();
let llmConfig = {};
let searchBusy = false;

const $ = (id) => document.getElementById(id);

function store() {
  try {
    const data = JSON.parse(localStorage.getItem(KEY) || "null");
    if (!data || !data.resume_text) return data;
    const compact = absorbResume(data);
    localStorage.setItem(KEY, JSON.stringify(compact));
    mirror();
    return compact;
  } catch (_) {
    return null;
  }
}
function saveStore(data) {
  localStorage.setItem(KEY, JSON.stringify(absorbResume(data)));
  mirror();
}
function loadJobs() {
  return jobCache;
}
function trackedJobs(rows) {
  return (rows || []).filter((job) => job && job.status && job.status !== "new");
}

function saveJobs(rows) {
  jobCache = Array.isArray(rows) ? rows : [];
  const snapshot = trackedJobs(jobCache);
  persistChain = persistChain
    .then(async () => {
      const db = await openStore();
      await writeSavedJobs(db, snapshot);
    })
    .catch(() => {});
  return persistChain;
}

function mirror() {
  openStore().then((db) => db.put("kv", store(), "setup")).catch(() => {});
}

function profileFrom(data) {
  return {
    name: data.name || "",
    resume_text: data.resume_text || "",
    skills: data.skills || [],
    titles: data.titles || [],
    years: data.years,
    roles: data.roles || [],
    locations: data.locations || [],
    lookback_days: Number(data.lookback_days || 14),
    max_years: data.max_years ? Number(data.max_years) : null,
    remote_only: !!data.remote_only,
    sponsorship_needed: !!data.sponsorship_needed,
    min_salary: data.min_salary ? Number(data.min_salary) : null,
    skip_leadership: !!data.skip_leadership,
    downrank: data.downrank || [],
    feedback: data.feedback || null,
    hidden_roles: data.hidden_roles || [],
    hidden_phrases: data.hidden_phrases || [],
  };
}

const VIEW_TITLES = { welcome: "Start", home: "Home", tracker: "Tracker", settings: "Settings" };

function show(view) {
  const name = view === "company" ? "Company" : (VIEW_TITLES[view] || "Home");
  document.title = `${name} · Job Autopilot`;
  document.querySelectorAll("[data-view]").forEach((el) => {
    el.hidden = el.dataset.view !== view;
  });
  document.querySelectorAll("[data-nav]").forEach((btn) => {
    const on = btn.dataset.nav === view;
    if (on) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
}

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 4200);
}

function renderChips(skills, selected) {
  const box = $("skillChips");
  box.innerHTML = "";
  skills.forEach((skill) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.textContent = skill;
    btn.setAttribute("aria-pressed", selected.includes(skill) ? "true" : "false");
    btn.addEventListener("click", () => {
      const next = selected.includes(skill) ? selected.filter((s) => s !== skill) : selected.concat(skill);
      selected.splice(0, selected.length, ...next);
      btn.setAttribute("aria-pressed", next.includes(skill) ? "true" : "false");
    });
    box.appendChild(btn);
  });
}

function transitionName(id, used) {
  const safe = String(id || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48);
  if (!safe || used.has(safe)) return "";
  used.add(safe);
  return `job-${safe}`;
}

function jobCard(job, trends = new Map(), usedNames = new Set()) {
  const li = document.createElement("li");
  li.className = "job";
  li.dataset.jobId = job.id || "";
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const name = transitionName(job.id, usedNames);
    if (name) li.style.viewTransitionName = name;
  }
  const href = safeHref(job.url);
  const title = document.createElement("h3");
  if (href) {
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = job.title || "Untitled role";
    title.appendChild(a);
  } else {
    title.textContent = job.title || "Untitled role";
  }
  const meta = document.createElement("p");
  meta.className = "meta";
  const pay = job.salary_min ? `$${Number(job.salary_min).toLocaleString()}` : "";
  const companyBtn = document.createElement("button");
  companyBtn.type = "button";
  companyBtn.className = "linkish";
  companyBtn.textContent = job.company || "Company";
  companyBtn.addEventListener("click", () => {
    location.hash = `#company/${encodeURIComponent(job.company || "")}`;
  });
  meta.append(companyBtn, document.createTextNode(` · ${[job.location_raw, (job.posted_at || "").slice(0, 10), pay].filter(Boolean).join(" · ")}`));
  let spark = null;
  const trend = trends.get(String(job.company || "").replace(/\s+/g, " ").trim().toLowerCase());
  if (trend && configCache.insights !== false) {
    spark = document.createElement("p");
    spark.className = "trend";
    spark.textContent = `New per week: ${trend.map((week) => week.new_jobs).join(" ")}`;
  }
  const score = document.createElement("p");
  score.className = "score";
  const scoreNum = document.createElement("span");
  const shownScore = job.ai ? job.display_score : job.match_score;
  score.style.setProperty("--p", String(shownScore ?? job.match_score ?? 0));
  scoreNum.textContent = shownScore == null ? "—" : String(shownScore);
  score.appendChild(scoreNum);
  let relation = null;
  if (job.relation) {
    relation = document.createElement("p");
    relation.className = "badge";
    relation.textContent = job.relation;
  }
  let badge = null;
  if (job.sponsorship && job.sponsorship !== "unknown") {
    badge = document.createElement("p");
    badge.className = "badge";
    badge.textContent = job.sponsorship === "yes" ? "Sponsorship listed" : "No sponsorship";
  }
  const why = document.createElement("p");
  why.className = "why";
  const hits = (job.why_matched || []).slice(0, 3).join(", ");
  const miss = (job.why_missing || []).slice(0, 3).join(", ");
  const bits = [];
  if (hits) bits.push(`Matches ${hits}`);
  if (miss) bits.push(`Missing ${miss}`);
  if (job.experience_line) bits.push(job.experience_line);
  why.textContent = bits.join(". ") || "Score is based on the role text and your profile.";
  const actions = document.createElement("div");
  actions.className = "row";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "btn";
  save.textContent = job.status === "saved" ? "Saved" : "Save";
  save.addEventListener("click", () => updateStatus(job.id, "saved"));
  const applied = document.createElement("button");
  applied.type = "button";
  applied.className = "btn primary";
  applied.textContent = job.status === "applied" ? "Applied" : "Mark applied";
  applied.addEventListener("click", () => updateStatus(job.id, "applied"));
  const hide = document.createElement("button");
  hide.type = "button";
  hide.className = "btn";
  hide.textContent = "Hide";
  hide.addEventListener("click", () => updateStatus(job.id, "hidden"));
  const unlike = document.createElement("button");
  unlike.type = "button";
  unlike.className = "btn";
  unlike.textContent = "Not relevant";
  unlike.addEventListener("click", () => {
    const data = store();
    if (data) {
      const generic = new Set(["senior", "junior", "manager", "engineer", "director", "staff", "lead", "head", "principal", "executive", "specialist", "associate", "coordinator", "analyst"]);
    const words = String(job.title || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 4 && !generic.has(w));
      data.downrank = [...new Set([...(data.downrank || []), ...words])].slice(-40);
      saveStore(data);
    }
    updateStatus(job.id, "hidden");
  });
  actions.append(save, applied, hide, unlike);
  if (href) {
    const open = document.createElement("a");
    open.className = "btn";
    open.href = href;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = "Apply";
    open.addEventListener("click", () => {
      sessionStorage.setItem("jobAutopilotPendingApply", job.id);
      const data = store();
      if (!data) return;
      data.feedback = noteFeedback(profileFrom(data), job, 1).feedback;
      saveStore(data);
    });
    actions.appendChild(open);
  }
  li.append(score, title, meta);
  if (spark) li.appendChild(spark);
  if (relation) li.appendChild(relation);
  if (badge) li.appendChild(badge);
  li.append(why, actions);
  const profile = store() || {};
  appendAiDetails(job, li, (row) => { runAi([row]); }, profile.resume_text || "");
  appendApplyKit(job, li, profile.resume_text || "", profile.name || "");
  appendInterviewPrep(job, li, profile.resume_text || "");
  return li;
}

function updateStatus(id, status) {
  const current = loadJobs().find((job) => job.id === id);
  const rows = loadJobs().map((job) => (job.id === id ? markJob(job, status) : job));
  saveJobs(rows);
  const data = store();
  if (data && current && current.status !== status && (status === "saved" || status === "applied" || status === "hidden")) {
    data.feedback = noteFeedback(profileFrom(data), current, status === "hidden" ? -1 : 1).feedback;
    saveStore(data);
  }
  paint();
}

function paintFamily(data) {
  const box = $("familyChips");
  if (!box) return;
  box.innerHTML = "";
  const chips = familyChips(profileFrom(data));
  const label = $("alsoSearching");
  if (label) label.hidden = chips.length === 0;
  chips.forEach((chip) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.textContent = chip.label;
    btn.setAttribute("aria-label", `Stop searching ${chip.label}`);
    btn.addEventListener("click", () => {
      const next = store();
      if (!next) return;
      if (chip.kind === "synonym") {
        next.hidden_phrases = [...new Set([...(next.hidden_phrases || []), chip.label.toLowerCase()])];
      } else {
        next.hidden_roles = [...new Set([...(next.hidden_roles || []), chip.id])];
      }
      saveStore(next);
      paint();
    });
    box.appendChild(btn);
  });
  const tuned = $("tunedNote");
  if (tuned) tuned.hidden = !(data.feedback && ((data.feedback.posCount || 0) + (data.feedback.negCount || 0)));
}

function fixButton(reason) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  if (reason.fix === "widen") {
    button.textContent = "Look back 30 days";
    button.addEventListener("click", () => {
      const next = store();
      if (!next) return;
      next.lookback_days = 30;
      saveStore(next);
      if ($("lookback")) $("lookback").value = "30";
      paint();
    });
    return button;
  }
  if (reason.fix === "show-possible") {
    button.textContent = "Show possible matches";
    button.addEventListener("click", () => {
      $("showPossible").checked = true;
      paint();
    });
    return button;
  }
  if (reason.fix === "clear-remote") {
    button.textContent = "Clear remote only";
    button.addEventListener("click", () => {
      $("remoteFilter").checked = false;
      paint();
    });
    return button;
  }
  if (reason.fix === "clear-score") {
    button.textContent = "Clear minimum score";
    button.addEventListener("click", () => {
      $("minScore").value = "0";
      paint();
    });
    return button;
  }
  if (reason.fix === "clear-since") {
    button.textContent = "Show all dates";
    button.addEventListener("click", () => {
      $("sinceVisit").checked = false;
      paint();
    });
    return button;
  }
  button.textContent = "Clear filter";
  button.addEventListener("click", () => {
    $("jobSearch").value = "";
    $("statusFilter").value = "all";
    paint();
  });
  return button;
}

function controlsFromPage() {
  return {
    status: $("statusFilter").value,
    q: $("jobSearch").value,
    minScore: Number($("minScore").value || 0),
    remoteOnly: $("remoteFilter").checked,
    since: $("sinceVisit").checked ? (localStorage.getItem("jobAutopilotLastVisit") || "") : "",
    showPossible: $("showPossible").checked,
  };
}

function macMode() {
  return isMacHost(location.hostname);
}

function showAiBanner(message) {
  const banner = $("aiBanner");
  if (!banner) return;
  banner.hidden = !message;
  banner.textContent = message || "";
}

async function llmStorage() {
  const db = await openStore();
  return {
    get: (key) => db.get("kv", key),
    set: (key, value) => db.put("kv", value, key),
    getCache: (key) => db.get("llm-cache", key),
    setCache: (row) => db.put("llm-cache", row),
    prune: async (max) => {
      const rows = await db.getAll("llm-cache");
      if (!Array.isArray(rows) || rows.length <= max) return;
      rows.sort((a, b) => (a?.at || 0) - (b?.at || 0));
      await Promise.all(rows.slice(0, rows.length - max).filter((row) => row?.key).map((row) => db.delete("llm-cache", row.key)));
    },
  };
}

async function currentAiSettings() {
  const data = store() || {};
  const secret = macMode() ? null : await openSecret(await llmStorage());
  const provider = data.ai_provider || $("aiProvider")?.value || "";
  return {
    consent: !!data.ai_consent,
    exactDescription: !!data.ai_exact_description,
    provider,
    model: data.ai_model || (llmConfig.llm_models || {})[provider] || "",
    apiKey: !macMode() && secret?.provider === provider ? (secret.apiKey || "") : "",
    mac: macMode(),
    dailyCap: Number(llmConfig.llm_daily_cap) || DAILY_CAP_DEFAULT,
    topN: Number(llmConfig.llm_top_n) || TOP_N_DEFAULT,
    concurrency: Number((llmConfig.llm_concurrency || {})[provider]) || 1,
  };
}

async function runAi(jobs) {
  const data = store();
  if (!data?.ai_consent) {
    showAiBanner("Turn on consent before any AI request.");
    return;
  }
  const settings = await currentAiSettings();
  if (!settings.provider || !settings.model) {
    showAiBanner("Choose a provider and a model.");
    return;
  }
  const progress = $("aiProgressHome");
  try {
    const result = await scoreJobs(jobs, profileFrom(data), settings, {
      storage: await llmStorage(),
      fetchDescription: (job) => fetchJobText(job),
      onProgress({ total, done }) {
        const text = `AI scoring ${total} jobs, ${done} done`;
        if (progress) progress.textContent = text;
        if ($("aiProgress")) $("aiProgress").textContent = text;
      },
    });
    const rows = loadJobs().map((job) => {
      const ai = result.results.get(job.id);
      return ai ? { ...job, ai } : job;
    });
    saveJobs(rows);
    showAiBanner(result.ok ? "" : result.message);
  } catch (error) {
    showAiBanner(error.message || "AI scoring stopped. Local scores are unchanged.");
  }
  paint();
}

function paintAi(data) {
  const select = $("aiProvider");
  if (!select) return;
  const options = providersForMode(macMode());
  if (select.options.length !== options.length) {
    select.replaceChildren();
    options.forEach((provider) => {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label;
      select.appendChild(option);
    });
  }
  if (document.activeElement !== select) select.value = data.ai_provider || options[0]?.id || "";
  const key = $("aiKey");
  if (key && document.activeElement !== key) key.value = "";
  const note = $("aiKeyNote");
  if (note) {
    note.textContent = macMode()
      ? "The key stays in the macOS Keychain."
      : "The key stays in this browser, encrypted.";
  }
  const model = $("aiModel");
  if (model && document.activeElement !== model) {
    model.value = data.ai_model || "";
    model.placeholder = (llmConfig.llm_models || {})[select.value] || "Model from the provider, or type one";
  }
  const consent = $("aiConsent");
  if (consent && document.activeElement !== consent) consent.checked = !!data.ai_consent;
  const exact = $("aiExact");
  if (exact && document.activeElement !== exact) exact.checked = !!data.ai_exact_description;
  const chosen = options.find((item) => item.id === select.value);
  const lead = $("aiConsentLead");
  if (lead) {
    lead.textContent = `Your full resume (contact details removed) and each job description are sent to ${chosen?.label || "the provider"}.`;
  }
  const terms = $("aiConsentText");
  if (terms && !terms.dataset.ready) {
    terms.textContent = "Free tiers of some providers may use inputs to improve their products. ";
    options.forEach((provider) => {
      const link = document.createElement("a");
      link.href = provider.terms;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = provider.termsLabel;
      terms.append(link, document.createTextNode(". "));
    });
    terms.dataset.ready = "yes";
  }
  const previewJobs = loadJobs().slice(0, 4);
  const promptSettings = { exactDescription: !!data.ai_exact_description, topN: TOP_N_DEFAULT };
  const prompt = buildPrompt(profileFrom(data), previewJobs, promptSettings);
  if ($("aiPreview")) $("aiPreview").textContent = JSON.stringify({ system: prompt.system, user: prompt.user }, null, 2);
  const top = Math.max(1, Math.min(50, Number(llmConfig.llm_top_n) || TOP_N_DEFAULT));
  if ($("aiEstimate")) {
    $("aiEstimate").textContent = `About ${estimateTokens(profileFrom(data), loadJobs(), { ...promptSettings, topN: top })} tokens for up to ${top} jobs. Daily cap ${Number(llmConfig.llm_daily_cap) || DAILY_CAP_DEFAULT} jobs.`;
  }
}

function paint() {
  const data = store();
  if (!data) {
    show("welcome");
    return;
  }
  const view = location.hash.replace("#", "") || "home";
  if (view === "welcome") {
    show("home");
  } else {
    show(["home", "tracker", "settings"].includes(view) ? view : "home");
  }
  const jobs = loadJobs();
  const selected = selectJobs(jobs, profileFrom(data), controlsFromPage());
  const visible = selected.rows;
  $("jobCount").textContent = plural(visible.length, "role");
  const live = $("statusLive");
  if (live && !String(live.textContent || "").startsWith("Searching")) {
    live.textContent = `Found ${plural(visible.length, "role")}.`;
  }
  paintFamily(data);
  const ul = $("jobList");
  ul.innerHTML = "";
  const extra = selected.reasons.find((reason) => reason.id === "widen-more");
  if (extra && visible.length < 20) {
    const note = document.createElement("li");
    note.className = "empty";
    const msg = document.createElement("p");
    msg.textContent = extra.text;
    note.appendChild(msg);
    note.appendChild(fixButton(extra));
    ul.appendChild(note);
  }
  if (!visible.length) {
    const li = document.createElement("li");
    li.className = "empty";
    const msg = document.createElement("p");
    const reason = selected.reasons[0];
    msg.textContent = reason
      ? reason.text
      : (jobs.length ? "Nothing matches that filter." : "No roles yet. Search asks for up to 300 matching roles.");
    li.appendChild(msg);
    if (reason) li.appendChild(fixButton(reason));
    ul.appendChild(li);
  } else {
    const presented = visible.map((job) => presentScore(job))
      .sort((a, b) => (b.display_score || 0) - (a.display_score || 0) || String(b.posted_at).localeCompare(String(a.posted_at)));
    const sections = [
      ["Closest matches", "strong"],
      ["Good matches", "good"],
      ["Stretch roles", "stretch"],
    ];
    const used = new Set();
    let shown = 0;
    sections.forEach(([label, tier]) => {
      const rows = presented.filter((job) => job.tier === tier);
      if (!rows.length || shown >= 80) return;
      const head = document.createElement("li");
      head.className = "section-label";
      head.textContent = label;
      ul.appendChild(head);
      rows.slice(0, 80 - shown).forEach((job) => {
        ul.appendChild(jobCard(job, new Map(), used));
        shown += 1;
      });
    });
    presented.filter((job) => !["strong", "good", "stretch"].includes(job.tier)).slice(0, Math.max(0, 80 - shown)).forEach((job) => {
      ul.appendChild(jobCard(job, new Map(), used));
    });
  }
  const stats = kpis(jobs);
  $("kpi").textContent = `${stats.applied_week} applied this week · ${stats.interviews} interviews · ${stats.offers} offers · ${stats.response_rate}% response`;
  paintBoard(jobs);
  $("settingsName").textContent = data.name || "Your search";
  if ($("planLine")) $("planLine").textContent = `Plan: ${PLAN}. Matching, the tracker, export, and delete stay available either way.`;
  window.postMessage({
    type: "job-autopilot-profile",
    profile: {
      name: data.name || "",
      location: (data.locations || [])[0] || "",
      role: (data.roles || [])[0] || "",
    },
  }, location.origin);
  $("resumeName").textContent = data.resume_name || "No resume stored";
  $("liveBoards").checked = !!data.live_boards;
  $("mailOptIn").checked = !!data.mail_opt_in;
  const feedAt = localStorage.getItem("jobAutopilotFeedAt");
  const age = feedAt ? Date.now() - new Date(feedAt).getTime() : 0;
  $("feedAge").hidden = !(feedAt && age > 12 * 3600000);
  $("offline").hidden = navigator.onLine;
  if ($("usajobsEmail") && document.activeElement !== $("usajobsEmail")) $("usajobsEmail").value = data.usajobs_email || "";
  if ($("usajobsKey") && document.activeElement !== $("usajobsKey")) $("usajobsKey").value = data.usajobs_key || "";
  paintAi(data);
  paintStorage();
}

function paintStorage() {
  const line = $("storageUsed");
  if (!line) return;
  measureStorage().then((bytes) => {
    line.textContent = `Storage used: ${formatMegabytes(bytes)}`;
  }).catch(() => {});
}

function paintBoard(jobs) {
  const board = $("board");
  board.innerHTML = "";
  const grouped = STATUSES.map((status) => {
    const col = document.createElement("section");
    col.className = "col";
    const h = document.createElement("h3");
    const rows = jobs.filter((j) => j.status === status);
    h.textContent = `${status} · ${rows.length}`;
    col.addEventListener("dragover", (event) => event.preventDefault());
    col.addEventListener("drop", () => { if (dragId) updateStatus(dragId, status); });
    col.appendChild(h);
    rows.slice(0, 20).forEach((job) => {
      const card = document.createElement("article");
      card.tabIndex = 0;
      card.addEventListener("keydown", (event) => {
        const idx = STATUSES.indexOf(job.status);
        if (event.key === "ArrowRight" && idx < STATUSES.length - 1) {
          event.preventDefault();
          updateStatus(job.id, STATUSES[idx + 1]);
        }
        if (event.key === "ArrowLeft" && idx > 0) {
          event.preventDefault();
          updateStatus(job.id, STATUSES[idx - 1]);
        }
      });
      card.draggable = true;
      card.addEventListener("dragstart", () => { dragId = job.id; });
      const t = document.createElement("strong");
      t.textContent = job.title || "Role";
      const p = document.createElement("p");
      p.textContent = job.company || "";
      const due = followUpDue(job);
      if (due) {
        const note = document.createElement("p");
        note.className = "due";
        note.textContent = `Follow up · ${due} days`;
        card.append(t, p, note);
      } else {
        card.append(t, p);
      }
      const sel = document.createElement("select");
      sel.setAttribute("aria-label", `Status for ${job.title || "role"}`);
      STATUSES.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s;
        opt.textContent = s;
        if (s === job.status) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", () => updateStatus(job.id, sel.value));
      const notes = document.createElement("textarea");
      notes.value = job.notes || "";
      notes.placeholder = "Notes";
      notes.addEventListener("change", () => {
        const next = loadJobs().map((row) => (row.id === job.id ? { ...row, notes: notes.value } : row));
        saveJobs(next);
      });
      const cal = document.createElement("button");
      cal.type = "button";
      cal.className = "btn";
      cal.textContent = "Calendar";
      cal.addEventListener("click", () => {
        const blob = new Blob([icsFor(job)], { type: "text/calendar" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "interview.ics";
        a.click();
      });
      card.append(sel, notes, cal);
      if (job.status === "applied") {
        [7, 14].forEach((days) => {
          const follow = document.createElement("button");
          follow.type = "button";
          follow.className = "btn";
          follow.textContent = `Follow-up · ${days} days`;
          const draft = document.createElement("textarea");
          draft.hidden = true;
          draft.value = followUpDraft(job, days);
          follow.addEventListener("click", () => { draft.hidden = false; });
          card.append(follow, draft);
        });
      }
      col.appendChild(card);
    });
    return col;
  });
  grouped.forEach((col) => board.appendChild(col));
}

async function rankInWorker(jobs, profile) {
  if (typeof Worker === "undefined" || (jobs || []).length <= 40) return rankAll(jobs, profile);
  try {
    const ranked = await new Promise((resolve, reject) => {
      const worker = new Worker(new URL("./rank.worker.js", import.meta.url), { type: "module" });
      const timer = setTimeout(() => {
        worker.terminate();
        reject(new Error("The match took too long."));
      }, 8000);
      worker.onmessage = (event) => {
        clearTimeout(timer);
        worker.terminate();
        resolve(event.data);
      };
      worker.onerror = () => {
        clearTimeout(timer);
        worker.terminate();
        reject(new Error("Matching failed."));
      };
      const safe = { ...(profile || {}) };
      delete safe.resume_text;
      delete safe.resume_file;
      worker.postMessage({ jobs, profile: safe });
    });
    return ranked;
  } catch (_) {
    return rankAll(jobs, profile);
  }
}

function tidyJob(job) {
  return { ...job, company: cleanCompany(job.company) };
}

async function loadUsaJobs(data) {
  const key = String(data.usajobs_key || "").trim();
  const email = String(data.usajobs_email || "").trim();
  if (!key || !email) return [];
  const roles = (data.roles || []).slice(0, 2);
  const places = (data.locations || []).map((place) => String(place || "").trim()).filter((place) => place && !/remote/i.test(place)).slice(0, 2);
  const where = places.length ? places : ["United States"];
  const jobs = [];
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    for (const role of roles) {
      for (const place of where) {
        const url = new URL("https://data.usajobs.gov/api/search");
        url.searchParams.set("Keyword", role);
        url.searchParams.set("LocationName", place);
        url.searchParams.set("ResultsPerPage", "25");
        const response = await fetch(url, {
          headers: { "Authorization-Key": key, "User-Agent": email },
          signal: controller.signal,
        });
        if (!response.ok) continue;
        const body = await response.json();
        const items = body?.SearchResult?.SearchResultItems || [];
        items.forEach((item) => {
          const row = item.MatchedObjectDescriptor || {};
          const loc = row.PositionLocationDisplay || "";
          const summary = row.UserArea?.Details?.JobSummary || "";
          jobs.push(tidyJob({
            id: `usajobs-${item.MatchedObjectId || row.PositionURI || jobs.length}`,
            source: "usajobs",
            company: row.OrganizationName || "USAJobs",
            title: row.PositionTitle || "",
            url: row.PositionURI || "",
            location_raw: loc,
            locations: [{ city: loc, region: "", country: "United States", remote: /remote/i.test(loc) ? "remote" : "" }],
            posted_at: row.PublicationStartDate || "",
            updated_at: row.PublicationStartDate || "",
            description_text: String(summary).replace(/\s+/g, " ").trim().slice(0, 1500),
          }));
        });
      }
    }
  } finally {
    clearTimeout(timer);
  }
  return jobs;
}

async function searchNow() {
  if (searchBusy) return;
  const data = store();
  if (!data) return;
  const live = $("statusLive");
  searchBusy = true;
  live.textContent = "Searching…";
  try {
    const profile = profileFrom(data);
    let vector = null;
    try {
      vector = await fetchProfileVector(profile);
    } catch (_) {
      vector = null;
    }
    const rankingProfile = vector
      ? { ...profile, vector }
      : { ...profile, rank_without_embeddings: true };
    const candidates = await fetchCandidates(profile);
    let government = [];
    try {
      government = await loadUsaJobs(data);
    } catch (_) {
      government = [];
    }
    const pool = searchPool(candidates.concat(government), profile).slice(0, 300);
    const ranked = await rankInWorker(pool, rankingProfile);
    const collapsed = collapsePostings(ranked).map((job) => {
      const old = loadJobs().find((row) => row.id === job.id);
      return old ? { ...job, status: old.status, notes: old.notes, applied_at: old.applied_at } : { ...job, status: "new" };
    });
    const kept = loadJobs().filter((old) => !collapsed.some((job) => job.id === old.id) && old.status && old.status !== "new");
    saveJobs(collapsePostings(collapsed.concat(kept)));
    const selected = selectJobs(loadJobs(), profile, controlsFromPage());
    live.textContent = `Found ${plural(selected.rows.length, "role")}.`;
    if (data.notify && "Notification" in window && Notification.permission === "granted") {
      new Notification("Job search finished", { body: plural(selected.rows.length, "new role") });
    }
    location.hash = "#home";
    paint();
  } catch (err) {
    live.textContent = err.message || "Search failed. Your saved roles are unchanged.";
  } finally {
    searchBusy = false;
  }
}

function bootWelcome() {
  const selected = [];
  $("resumeFile").addEventListener("change", async () => {
    const file = $("resumeFile").files[0];
    $("resumeError").textContent = "";
    if (!file) return;
    try {
      const pdfjs = await import("pdfjs-dist");
      pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).href;
      const mammoth = await import("mammoth");
      const got = await readResumeFile(file, { pdfjs, mammoth });
      const skills = got.skills.length ? got.skills : skillsFromText(got.text);
      selected.splice(0, selected.length, ...skills);
      renderChips(skills.length ? skills : ["Add a skill in settings later"], selected);
      $("personName").value = guessName(got.text);
      $("roleInput").value = suggestTitles(got.text).join(", ");
      const parsed = profileFromResume(got.text);
      sessionStorage.setItem(DRAFT, JSON.stringify({
        name: file.name,
        skills: parsed.skills,
        titles: parsed.titles,
        years: parsed.years,
        text: String(got.text || "").slice(0, 20000),
      }));
      $("resumeFile").value = "";
      $("stepResume").hidden = true;
      $("stepPrefs").hidden = false;
    } catch (err) {
      $("resumeError").textContent = err.message || "Could not read that file.";
    }
  });
  $("saveSetup").addEventListener("click", () => {
    const runs = parseRunsPerDay($("runs").value);
    if (!runs.ok) {
      $("runsError").textContent = runs.error;
      $("runs").focus();
      return;
    }
    const draft = JSON.parse(sessionStorage.getItem(DRAFT) || "{}");
    const roles = $("roleInput").value.split(",").map((s) => s.trim()).filter(Boolean);
    const locations = $("whereInput").value.split(",").map((s) => s.trim()).filter(Boolean);
    if (!draft.name) {
      $("runsError").textContent = "Add a resume first.";
      return;
    }
    if (!roles.length || !locations.length) {
      $("runsError").textContent = "Add at least one title and one place.";
      return;
    }
    saveStore({
      name: $("personName").value.trim(),
      resume_name: draft.name,
      resume_text: String(draft.text || "").slice(0, 20000),
      skills: selected.length ? selected.slice() : (draft.skills || []),
      titles: draft.titles || [],
      years: draft.years || 0,
      roles,
      locations,
      lookback_days: Number($("lookback").value || 14),
      max_years: $("maxYears").value,
      remote_only: $("remoteOnly").checked,
      sponsorship_needed: $("sponsor").checked,
      skip_leadership: $("skipLeadership").checked,
      min_salary: $("salary").value,
      runs_per_day: runs.value,
      mail_opt_in: false,
      live_boards: false,
      notify: false,
    });
    sessionStorage.removeItem(DRAFT);
    location.hash = "#home";
    paint();
    searchNow();
  });
}

function sourceLabel(source) {
  if (source === "linkedin") return "LinkedIn";
  if (source === "google_jobs") return "Google Jobs";
  if (source === "jobright") return "Jobright";
  return source || "saved pages";
}

function importCaptured(incoming) {
  if (!incoming.length) {
    toast("No saved jobs are waiting in the extension.");
    return;
  }
  const saved = incoming.map((job) => ({
    ...job,
    status: job.status && job.status !== "new" ? job.status : "saved",
  }));
  saveJobs(mergeSources([{ jobs: loadJobs() }, { jobs: saved }]));
  paint();
  const names = [...new Set(incoming.map((job) => sourceLabel(job.source)))];
  toast(`Imported ${incoming.length} jobs from ${names.join(" and ")}.`);
}

function bootChrome() {
  document.querySelectorAll("[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => {
      location.hash = `#${btn.dataset.nav}`;
      paint();
    });
  });
  $("btnSearch").addEventListener("click", searchNow);
  $("showPossible").addEventListener("change", paint);
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || !event.data) return;
    if (event.data.type === "job-autopilot-hello" && event.data.nonce) {
      window.postMessage({ type: "job-autopilot-ack", nonce: event.data.nonce }, location.origin);
      return;
    }
    if (event.data.type === "job-autopilot-jobs" && Array.isArray(event.data.jobs)) {
      importCaptured(event.data.jobs);
    }
  });
  $("btnImportCapture").addEventListener("click", () => {
    const id = $("extensionId").value.trim();
    const runtime = globalThis.chrome && globalThis.chrome.runtime;
    if (!id || !runtime || !runtime.sendMessage) {
      toast("Add the extension id from the browser’s extensions page, then open this site in that browser.");
      return;
    }
    runtime.sendMessage(id, { type: "flush" }, (response) => {
      importCaptured((response && response.jobs) || []);
    });
  });
  $("jobSearch").addEventListener("input", paint);
  $("statusFilter").addEventListener("change", paint);
  $("minScore").addEventListener("input", paint);
  $("remoteFilter").addEventListener("change", paint);
  $("sinceVisit").addEventListener("change", paint);
  $("btnExcel").addEventListener("click", () => {
    const blob = jobsToXlsx(loadJobs());
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `Jobs_${new Date().toISOString().slice(0, 10)}.xlsx`;
    a.click();
  });
  $("btnExport").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify({ setup: store(), jobs: loadJobs() }, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "job-autopilot-export.json";
    a.click();
  });
  $("importFile").addEventListener("change", async () => {
    const file = $("importFile").files[0];
    if (!file) return;
    const data = JSON.parse(await file.text());
    if (data.setup) saveStore(data.setup);
    if (data.jobs) saveJobs(data.jobs);
    toast("Import finished.");
    paint();
  });
  $("btnClearData").addEventListener("click", () => {
    if (!confirm("Clear app data stored in this browser?")) return;
    clearAppData().then(() => {
      jobCache = [];
      location.hash = "#welcome";
      location.reload();
    });
  });
  $("btnErase").addEventListener("click", () => {
    if (!confirm("Erase the resume, roles, and tracker stored in this browser?")) return;
    localStorage.removeItem(KEY);
    localStorage.removeItem(JOBS);
    sessionStorage.removeItem(DRAFT);
    jobCache = [];
    openStore().then(async (db) => {
      await db.clear("jobs");
      await db.delete("kv", "setup");
      await db.delete("kv", "jobs");
    }).catch(() => {});
    location.hash = "#welcome";
    paint();
  });
  $("liveBoards").addEventListener("change", () => {
    const data = store();
    if (!data) return;
    data.live_boards = $("liveBoards").checked;
    saveStore(data);
  });
  $("btnLocalScout").addEventListener("click", async () => {
    const data = store();
    if (!data?.live_boards) {
      toast("Turn on live boards on this computer first. The public site never scrapes.");
      return;
    }
    if (location.protocol === "file:" || location.hostname.endsWith("github.io")) {
      toast("Live board search only runs in the Mac app.");
      return;
    }
    const res = await fetch("/api/scout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ live_boards: true }),
    });
    toast(res.ok ? "Local search started." : "The Mac app did not start a search.");
  });
  $("btnNotify").addEventListener("click", async () => {
    if (!("Notification" in window)) {
      toast("This browser does not show notifications.");
      return;
    }
    const perm = await Notification.requestPermission();
    const data = store();
    if (data) {
      data.notify = perm === "granted";
      saveStore(data);
    }
    toast(perm === "granted" ? "Notifications are on for this browser." : "Notifications stay off.");
  });
  $("btnMail").addEventListener("click", async () => {
    const data = store();
    if (!data?.mail_opt_in) {
      toast("Turn on mail sync first. It stays off until you choose it.");
      return;
    }
    if (location.hostname.endsWith("github.io") || location.protocol === "file:") {
      toast("Mail sync runs in the Mac app, not on the public site.");
      return;
    }
    const res = await fetch("/api/sync", { method: "POST" });
    toast(res.ok ? "Mail sync started." : "The Mac app did not start mail sync.");
  });
  $("mailOptIn").addEventListener("change", () => {
    const data = store();
    if (!data) return;
    data.mail_opt_in = $("mailOptIn").checked;
    saveStore(data);
  });
  window.addEventListener("offline", () => { $("offline").hidden = false; });
  window.addEventListener("online", () => { $("offline").hidden = true; });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    const id = sessionStorage.getItem("jobAutopilotPendingApply");
    if (!id) return;
    $("applyAsk").hidden = false;
    $("applyAsk").dataset.id = id;
  });
  const saveAi = (patch) => {
    const data = store();
    if (!data) return;
    Object.assign(data, patch);
    saveStore(data);
    paint();
  };
  $("aiProvider").addEventListener("change", () => saveAi({ ai_provider: $("aiProvider").value }));
  $("aiModel").addEventListener("change", () => saveAi({ ai_model: $("aiModel").value.trim() }));
  $("aiConsent").addEventListener("change", () => saveAi({ ai_consent: $("aiConsent").checked }));
  $("aiExact").addEventListener("change", () => saveAi({ ai_exact_description: $("aiExact").checked }));
  $("aiKey").addEventListener("change", async () => {
    const value = $("aiKey").value.trim();
    $("aiKey").value = "";
    if (!value) return;
    const provider = $("aiProvider").value;
    try {
      if (macMode()) await macSaveKey(provider, value);
      else {
        const storage = await llmStorage();
        await sealSecret(storage, { provider, apiKey: value });
      }
      toast("Key saved on this device.");
    } catch (error) {
      showAiBanner(error.message || "The key could not be saved.");
    }
  });
  $("aiTest").addEventListener("click", async () => {
    try {
      const settings = await currentAiSettings();
      if (!settings.consent) {
        showAiBanner("Turn on consent before any AI request.");
        return;
      }
      const models = await listModels(settings);
      const list = $("aiModelList");
      list.replaceChildren();
      const suggested = (llmConfig.llm_models || {})[settings.provider] || "";
      [suggested, ...models].filter(Boolean).forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        list.appendChild(option);
      });
      showAiBanner("");
      toast(models.length ? "Key works." : "The key was accepted.");
    } catch (error) {
      showAiBanner(error.message || "The provider could not be reached. Local scores are unchanged.");
    }
  });
  $("aiScore").addEventListener("click", () => { runAi(loadJobs()); });
  $("resetFeedback").addEventListener("click", () => {
    const data = store();
    if (!data) return;
    const next = resetFeedback(data);
    delete next.feedback;
    saveStore(next);
    paint();
  });
  $("applyYes").addEventListener("click", () => {
    const id = $("applyAsk").dataset.id;
    if (id) updateStatus(id, "applied");
    sessionStorage.removeItem("jobAutopilotPendingApply");
    $("applyAsk").hidden = true;
  });
  $("applyNo").addEventListener("click", () => {
    sessionStorage.removeItem("jobAutopilotPendingApply");
    $("applyAsk").hidden = true;
  });
  if (!localStorage.getItem("jobAutopilotLastVisit")) {
    localStorage.setItem("jobAutopilotLastVisit", new Date(0).toISOString());
  }
  $("btnHealth").addEventListener("click", async () => {
    try {
      const res = await fetch("/api/health");
      const body = await res.json();
      $("healthOut").textContent = body.ok ? "Mac app is running." : "Mac app reported a problem.";
    } catch (_) {
      $("healthOut").textContent = "No Mac app on this address. The public site does not need it.";
    }
  });
  const saveGovernment = () => {
    const data = store();
    if (!data || !$("usajobsEmail")) return;
    data.usajobs_email = $("usajobsEmail").value.trim();
    data.usajobs_key = $("usajobsKey").value.trim();
    saveStore(data);
  };
  $("usajobsEmail").addEventListener("change", saveGovernment);
  $("usajobsKey").addEventListener("change", saveGovernment);
  window.addEventListener("hashchange", paint);
  const help = $("shortcutHelp");
  if (help) help.textContent = SHORTCUTS.map(([keys, action]) => `${keys}: ${action}`).join(". ");
  if ($("whatsNew") && localStorage.getItem("jobAutopilotWhatsNew") !== WHATS_NEW) $("whatsNew").hidden = false;
  $("whatsNewClose").addEventListener("click", () => {
    localStorage.setItem("jobAutopilotWhatsNew", WHATS_NEW);
    $("whatsNew").hidden = true;
  });
  const commands = [
    ["Search", () => { location.hash = "#home"; paint(); $("btnSearch").click(); }],
    ["Home", () => { location.hash = "#home"; paint(); }],
    ["Tracker", () => { location.hash = "#tracker"; paint(); }],
    ["Settings", () => { location.hash = "#settings"; paint(); }],
    ["Export", () => $("btnExport").click()],
    ["Score top matches", () => $("aiScore").click()],
  ];
  const paletteList = $("paletteList");
  const drawPalette = () => {
    const query = $("paletteInput").value.trim().toLowerCase();
    paletteList.replaceChildren();
    commands.filter(([label]) => label.toLowerCase().includes(query)).forEach(([label, run]) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", () => {
        $("palette").hidden = true;
        run();
      });
      item.appendChild(button);
      paletteList.appendChild(item);
    });
  };
  $("paletteInput").addEventListener("input", drawPalette);
  drawPalette();
  let chord = "";
  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("palette").hidden = !$("palette").hidden;
      if (!$("palette").hidden) {
        $("paletteInput").value = "";
        drawPalette();
        $("paletteInput").focus();
      }
      return;
    }
    if (event.key === "Escape") {
      $("palette").hidden = true;
      return;
    }
    if (typing) return;
    const key = event.key.toLowerCase();
    if (key === "g") {
      chord = "g";
      return;
    }
    if (chord === "g" && key === "t") {
      chord = "";
      location.hash = "#tracker";
      paint();
      return;
    }
    chord = "";
    if (key === "?") {
      $("whatsNew").hidden = false;
      return;
    }
    if (key === "/") {
      event.preventDefault();
      location.hash = "#home";
      paint();
      $("jobSearch").focus();
      return;
    }
    const cards = [...document.querySelectorAll("#jobList li.job")];
    if (!cards.length) return;
    let index = cards.findIndex((card) => card.classList.contains("picked"));
    if (index < 0) index = 0;
    if (key === "j" || key === "k") {
      index = key === "j" ? Math.min(cards.length - 1, index + 1) : Math.max(0, index - 1);
      cards.forEach((card) => card.classList.remove("picked"));
      cards[index].classList.add("picked");
      cards[index].scrollIntoView({ block: "nearest" });
      return;
    }
    if (!cards[index].classList.contains("picked")) cards[index].classList.add("picked");
    const press = (label) => [...cards[index].querySelectorAll("button")].find((button) => button.textContent === label)?.click();
    if (key === "s") press("Save");
    if (key === "a") press("Mark applied") || press("Applied");
    if (key === "h") press("Hide");
    if (key === "n") press("Not relevant");
  });
}

if ("serviceWorker" in navigator && location.protocol !== "file:") {
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}

bootWelcome();
bootChrome();
openStore().then(async (db) => {
  await enforceStorageBudget();
  const loaded = await migrateLegacyJobs(db);
  const tracked = trackedJobs(loaded);
  if (tracked.length !== loaded.length) await writeSavedJobs(db, tracked);
  jobCache = tracked;
  if (macMode()) migrateMacKeys().catch(() => {});
  const base = apiBase();
  if (base) {
    pullConfig(base).then((config) => { llmConfig = config || {}; paint(); }).catch(() => {});
  }
  if (!store()) {
    const setup = await db.get("kv", "setup");
    if (setup) saveStore(setup);
  }
}).catch(() => {}).finally(() => {
  paint();
  const dsn = document.querySelector('meta[name="sentry-dsn"]')?.content || "";
  loadSentry(dsn).catch(() => {});
});
