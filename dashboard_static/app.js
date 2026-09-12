(() => {
  const state = { track: "all", overview: null, view: "home" };

  const TITLES = {
    home: "Overview",
    apps: "Applications",
    mail: "Mail",
    setup: "Set up",
    settings: "Settings",
  };

  const $ = (id) => document.getElementById(id);

  function fmt(n) {
    return Number(n || 0).toLocaleString("en-US");
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function fmtWhen(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString("en-US", {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  }

  function pill(status) {
    const label = status || "unknown";
    return `<span class="pill ${label}">${label.replaceAll("_", " ")}</span>`;
  }

  async function getJSON(url) {
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  function setSyncNote(text, show) {
    const el = $("syncNote");
    el.hidden = !show;
    el.textContent = text;
  }

  function renderKpis(k, mail) {
    const cards = [
      { label: "Applied", value: k.applied, hint: `${fmt(k.companies)} companies`, accent: true },
      { label: "Waiting", value: k.waiting, hint: `${fmt(k.stale_applied_21d)} older than 21 days` },
      { label: "Interviews", value: k.interviews, hint: `${fmt(k.reached_interview)} reached overall` },
      { label: "Assessments", value: k.assessments, hint: `${fmt(k.reached_assessment)} reached overall` },
      { label: "Offers", value: k.offers, hint: "Matched offer mail" },
      { label: "Rejected", value: k.rejected, hint: `${k.response_rate}% heard back` },
    ];
    $("kpis").innerHTML = cards.map((c) => `
      <div class="kpi${c.accent ? " accent" : ""}">
        <div class="label">${c.label}</div>
        <div class="value">${fmt(c.value)}</div>
        <div class="hint">${c.hint}</div>
      </div>
    `).join("");

    const name = (state.overview && state.overview.candidate && state.overview.candidate.name) || "Job search";
    $("candidateName").textContent = name;
    const last = mail.last_processed_at || mail.last_received_at;
    const addr = mail.address || "";
    $("mailboxLine").textContent = addr
      ? `${addr} · last mailbox read ${fmtWhen(last)} · ${fmt(mail.job_related)} job emails / ${fmt(mail.scanned)} scanned`
      : `Last mailbox read ${fmtWhen(last)}`;
  }

  function renderFunnel(o) {
    const reached = o.funnel_reached || {};
    const current = o.funnel_current || {};
    const max = Math.max(reached.applied || o.kpis.applied || 1, 1);
    const rows = [
      ["Applied", reached.applied || o.kpis.applied, "applied"],
      ["Reached assessment", reached.assessment || 0, "assessment"],
      ["Reached interview", reached.interview || 0, "interview"],
      ["Reached offer", reached.offer || 0, "offer"],
      ["Currently waiting", current.waiting || 0, "waiting"],
      ["Rejected", current.rejected || 0, "rejected"],
    ];
    $("funnel").innerHTML = `<div class="funnel">${rows.map(([name, n, cls]) => {
      const w = n <= 0 ? 0 : Math.max(2, Math.round((n / max) * 100));
      return `<div class="funnel-row">
        <div class="name">${name}</div>
        <div class="bar ${cls}"><span style="width:${w}%"></span></div>
        <div class="n">${fmt(n)}</div>
      </div>`;
    }).join("")}</div>`;
  }

  function stackedChart(el, series, keys) {
    if (!series || !series.length) {
      el.innerHTML = `<p class="empty">No weekly series yet.</p>`;
      return;
    }
    const colors = {
      applied: "#1d3f5c",
      rejected: "#8a3d34",
      interview: "#2b5c49",
      assessment: "#8a5f1f",
      offer: "#6e5424",
    };
    const w = 640, h = 220, padL = 28, padR = 8, padT = 28, padB = 36;
    const innerW = w - padL - padR;
    const innerH = h - padT - padB;
    const max = Math.max(1, ...series.map((d) => keys.reduce((s, k) => s + (d[k] || 0), 0)));
    const bw = innerW / series.length;
    const bars = series.map((d, i) => {
      let y = padT + innerH;
      const stack = keys.map((k) => {
        const v = d[k] || 0;
        const bh = (v / max) * innerH;
        y -= bh;
        return `<rect x="${padL + i * bw + bw * 0.18}" y="${y}" width="${bw * 0.64}" height="${Math.max(0, bh)}" fill="${colors[k]}"></rect>`;
      }).join("");
      const label = i % 2 === 0 || series.length < 12 ? d.label : "";
      return `${stack}<text x="${padL + i * bw + bw / 2}" y="${h - 12}" text-anchor="middle" fill="#6c675e" font-size="10">${label}</text>`;
    }).join("");
    const legend = keys.map((k, i) =>
      `<g transform="translate(${padL + i * 92}, 12)">
         <rect width="9" height="9" fill="${colors[k]}"></rect>
         <text x="14" y="9" font-size="11" fill="#6c675e">${k}</text>
       </g>`
    ).join("");
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Weekly counts">${legend}${bars}</svg>`;
  }

  function renderInsights(list) {
    if (!list.length) {
      $("insights").innerHTML = `<p class="empty">Insights appear after mailbox sync has classified applications.</p>`;
      return;
    }
    $("insights").innerHTML = list.map((item) => `
      <div class="insight">
        <h3>${item.title}</h3>
        <div class="val">${item.value}</div>
        <p>${item.detail}</p>
      </div>
    `).join("");
  }

  function jobTable(rows, { editable = false } = {}) {
    if (!rows.length) return `<p class="empty">Nothing in this slice.</p>`;
    const head = `<tr>
      <th>Status</th><th>Role</th><th>Company</th><th>Score</th><th>Logged</th><th>Last mail</th><th></th>
    </tr>`;
    const body = rows.map((j) => {
      const statusCell = editable
        ? `<select class="status-select" data-id="${j.id}" data-track="${j.track}">
             ${["applied", "assessment", "interview", "offer", "rejected", "withdrawn"].map((s) =>
               `<option value="${s}"${s === j.status ? " selected" : ""}>${s}</option>`).join("")}
           </select>`
        : pill(j.status);
      const mail = j.last_mail_stage
        ? `${pill(j.last_mail_stage)} <span class="muted">${fmtDate(j.last_mail_at)}</span>`
        : `<span class="muted">${j.last_mail_at ? fmtDate(j.last_mail_at) : "—"}</span>`;
      return `<tr>
        <td>${statusCell}</td>
        <td class="title">${j.url ? `<a href="${j.url}" target="_blank" rel="noreferrer">${escapeHtml(j.title || "Untitled")}</a>` : escapeHtml(j.title || "Untitled")}
          <div class="muted">${escapeHtml(j.track || "")}${j.location ? " · " + escapeHtml(j.location) : ""}</div>
        </td>
        <td>${escapeHtml(j.company)}</td>
        <td>${j.match_score || "—"}</td>
        <td class="muted">${fmtDate(j.first_seen)}</td>
        <td>${mail}</td>
        <td>${j.source ? `<span class="muted">${escapeHtml(j.source)}</span>` : ""}</td>
      </tr>`;
    }).join("");
    return `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function renderCompanies(rows) {
    if (!rows.length) {
      $("companies").innerHTML = `<p class="empty">No matched applications yet.</p>`;
      return;
    }
    $("companies").innerHTML = rows.map((c) => `
      <div class="company-row">
        <div>${escapeHtml(c.company)}</div>
        <div class="meters">${c.n} apps · ${c.waiting} waiting · ${c.interviews} interviews · ${c.rejected} rejected</div>
      </div>
    `).join("");
  }

  function mailTable(events) {
    if (!events.length) return `<p class="empty">No job-related mail in this filter.</p>`;
    const head = `<tr><th>When</th><th>Stage</th><th>Subject</th><th>From</th><th>Match</th></tr>`;
    const body = events.map((e) => `<tr>
      <td class="muted">${fmtWhen(e.received_at)}</td>
      <td>${pill(e.stage)}${e.ambiguous ? ` <span class="pill">ambiguous</span>` : ""}</td>
      <td class="title">${escapeHtml(e.subject || "(no subject)")}
        ${e.snippet ? `<div class="muted">${escapeHtml(e.snippet)}</div>` : ""}
      </td>
      <td class="muted">${escapeHtml(e.from_addr)}</td>
      <td>${e.linked ? `<span class="pill applied">matched</span>` : `<span class="pill unlinked">unmatched</span>`}</td>
    </tr>`).join("");
    return `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  function renderTracks(tracks) {
    $("tracks").innerHTML = (tracks || []).map((t) => {
      const d = t.digest;
      const digestLine = d
        ? `Latest scout ${fmtWhen(d.generated_at)} · ${fmt(d.new_this_run)} new · ${fmt(d.shortlist)} on the shortlist`
        : "No recent scout yet — click Find jobs";
      return `<div class="track-card">
        <h3>${escapeHtml(t.label)}</h3>
        <div class="mini">
          <span><b>${fmt(t.funnel)}</b> applied</span>
          <span><b>${fmt(t.interview)}</b> interviews</span>
          <span><b>${fmt(t.assessment)}</b> assessments</span>
          <span><b>${fmt(t.rejected)}</b> rejected</span>
          <span><b>${fmt(t.new)}</b> new matches</span>
        </div>
        <p class="caption">${digestLine}</p>
      </div>`;
    }).join("") || `<p class="empty">Save setup, then click Find jobs.</p>`;
  }

  function renderTrackNav(tracks) {
    const host = $("trackSeg");
    if (!host) return;
    const current = state.track;
    const bits = [`<button class="seg-btn${current === "all" ? " is-on" : ""}" data-track="all" type="button">All</button>`];
    (tracks || []).forEach((t) => {
      bits.push(`<button class="seg-btn${current === t.id ? " is-on" : ""}" data-track="${escapeHtml(t.id)}" type="button">${escapeHtml(t.label)}</button>`);
    });
    host.innerHTML = bits.join("");
    host.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        state.track = btn.dataset.track;
        host.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-on", b === btn));
        await refresh();
      });
    });
  }

  function showScreen(id) {
    document.querySelectorAll(".screen").forEach((el) => el.classList.toggle("is-on", el.id === id));
  }

  function isConfigured() {
    return !(state.overview && state.overview.setup && !state.overview.setup.configured);
  }

  function routePath() {
    const raw = (location.hash || "#/").replace(/^#/, "");
    return raw.startsWith("/") ? raw : `/${raw}`;
  }

  function showView(name) {
    state.view = name;
    document.querySelectorAll(".view").forEach((el) => el.classList.toggle("is-on", el.id === `view-${name}`));
    document.querySelectorAll(".app-nav a").forEach((a) => {
      a.classList.toggle("is-on", a.dataset.view === name);
    });
  }

  async function applyRoute() {
    const path = routePath();
    const configured = isConfigured();
    if (!configured || path === "/setup" || path === "/settings") {
      const status = (state.overview && state.overview.setup) || await getJSON("/api/setup-status");
      fillSetup(status);
      $("btnSetupCancel").hidden = !configured;
      const heading = document.querySelector("#screen-setup h1");
      if (heading) heading.textContent = configured ? "Settings" : "Set up your job search";
      const lead = document.querySelector("#screen-setup .setup-lead");
      if (lead) {
        lead.textContent = configured
          ? "Update your resume, roles, location, schedule, or keys. Blank key fields keep the keys already saved."
          : "A local web app for any job seeker. Upload a resume, say what you want, pick a location, and add your own API keys. It finds postings, scores them against your resume, and tracks replies from email.";
      }
      showScreen("screen-setup");
      document.title = `${configured ? TITLES.settings : TITLES.setup} · Job Autopilot`;
      return;
    }
    showScreen("screen-app");
    const view = path === "/applications" ? "apps" : path === "/mail" ? "mail" : "home";
    showView(view);
    document.title = `${TITLES[view]} · Job Autopilot`;
  }

  function fillSetup(status) {
    const form = $("setupForm");
    if (!form || !status) return;
    const c = status.candidate || {};
    const map = {
      name: c.name,
      email: c.email,
      phone: c.phone,
      linkedin: c.linkedin,
      resume_pdf: status.resume_pdf,
      roles: (status.roles || []).join(", "),
      location: status.location || "United States",
      lookback_days: status.lookback_days || 7,
      runs_per_day: status.runs_per_day || 4,
      max_years_required: status.max_years_required || 3,
      llm_provider: status.llm_provider || "gemini",
      scraper_type: status.scraper_type && status.scraper_type !== "none" ? status.scraper_type : "jsearch",
    };
    Object.entries(map).forEach(([k, v]) => {
      if (form.elements[k] && v != null && v !== "") form.elements[k].value = v;
    });
    if (status.resume_ok) $("resumeHint").textContent = "A resume is already saved. Upload a new file only if you want to replace it.";
    toggleAdzuna();
  }

  function toggleAdzuna() {
    const wrap = $("adzunaIdWrap");
    const sel = $("scraperType");
    if (wrap && sel) wrap.hidden = sel.value !== "adzuna";
  }

  async function saveSetup(ev) {
    ev.preventDefault();
    const form = $("setupForm");
    const err = $("setupError");
    err.hidden = true;
    const fd = new FormData(form);
    const file = form.elements.resume_file?.files?.[0];
    if (file) fd.set("resume_file", file);
    else fd.delete("resume_file");
    try {
      $("btnSetupSave").disabled = true;
      const res = await fetch("/api/setup", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      if (data.status) {
        state.overview = state.overview || {};
        state.overview.setup = data.status;
      }
      if (location.hash !== "#/") location.hash = "#/";
      showScreen("screen-app");
      await refresh();
      await applyRoute();
    } catch (e) {
      err.hidden = false;
      err.textContent = e.message;
    } finally {
      $("btnSetupSave").disabled = false;
    }
  }

  function updateScoutButton(scout) {
    const btn = $("btnScout");
    if (!btn) return;
    if (scout && scout.running) {
      btn.disabled = true;
      btn.classList.add("busy");
      btn.textContent = "Finding jobs";
      setSyncNote("Searching boards and scoring against your resume. This can take several minutes.", true);
    } else {
      btn.disabled = false;
      btn.classList.remove("busy");
      btn.textContent = "Find jobs";
      if (scout && scout.error) setSyncNote(`Job search failed: ${scout.error}`, true);
      else if (scout && scout.result && scout.finished_at) {
        setSyncNote("Job search finished. The ledger and shortlist are updated.", true);
      }
    }
  }

  async function loadOverview() {
    const data = await getJSON(`/api/overview?track=${encodeURIComponent(state.track)}`);
    state.overview = data;
    if (data.setup && !data.setup.configured) {
      fillSetup(data.setup);
      if (location.hash !== "#/setup") location.hash = "#/setup";
      await applyRoute();
      return data;
    }
    renderTrackNav(data.tracks || []);
    renderKpis(data.kpis, data.mailbox);
    renderFunnel(data);
    stackedChart($("mailChart"), data.weekly_mail, ["applied", "rejected", "interview"]);
    const mail = data.mailbox;
    $("mailCaption").textContent =
      `Source: mailbox + job ledger · this week ${fmt(mail.receipts_this_week)} receipts vs ${fmt(mail.receipts_last_week)} last week`;
    stackedChart($("jobsChart"), data.weekly_jobs, ["applied", "rejected", "interview"]);
    renderInsights(data.insights || []);
    $("attention").innerHTML = jobTable(data.attention || []);
    renderCompanies(data.companies || []);
    renderTracks(data.tracks || []);
    updateSyncButton(data.sync);
    updateScoutButton(data.scout);
    return data;
  }

  async function loadPipeline() {
    const status = $("statusFilter").value;
    const q = $("jobSearch").value.trim();
    const url = `/api/pipeline?track=${encodeURIComponent(state.track)}&status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}&limit=80`;
    const data = await getJSON(url);
    $("pipeline").innerHTML = jobTable(data.jobs || [], { editable: true });
    $("pipelineCaption").textContent = `${fmt(data.total)} rows in this filter. Changing status writes through to SQLite.`;
    $("pipeline").querySelectorAll(".status-select").forEach((el) => {
      el.addEventListener("change", async () => {
        try {
          await postJSON("/api/jobs/mark", {
            id: el.dataset.id,
            track: el.dataset.track,
            status: el.value,
          });
          await refresh();
        } catch (err) {
          setSyncNote(err.message, true);
        }
      });
    });
  }

  async function loadMail() {
    const stage = $("mailStage").value;
    const linked = $("mailLinked").value;
    const data = await getJSON(
      `/api/mail?track=${encodeURIComponent(state.track)}&stage=${encodeURIComponent(stage)}&linked=${encodeURIComponent(linked)}&limit=60`
    );
    $("mail").innerHTML = mailTable(data.events || []);
    $("mailTableCaption").textContent = `${fmt(data.total)} job-related messages in this filter.`;
  }

  function updateSyncButton(sync) {
    const btn = $("btnSync");
    if (sync && sync.running) {
      btn.disabled = true;
      btn.classList.add("busy");
      btn.textContent = "Reading mailbox";
      setSyncNote("Scanning Gmail over IMAP. Statuses update only when a message clearly matches a posting.", true);
    } else {
      btn.disabled = false;
      btn.classList.remove("busy");
      btn.textContent = "Sync mailbox";
      if (sync && sync.error) {
        setSyncNote(`Mailbox sync failed: ${sync.error}`, true);
      } else if (sync && sync.result) {
        const bits = sync.result.map((r) =>
          `${r.track}: ${r.updated} updated, ${r.classified} classified`
        );
        setSyncNote(`Mailbox sync finished. ${bits.join(" · ")}`, true);
      }
    }
  }

  let pollTimer = null;
  async function pollSync() {
    const sync = await getJSON("/api/sync");
    updateSyncButton(sync);
    if (sync.running) pollTimer = setTimeout(pollSync, 2500);
    else {
      pollTimer = null;
      if (sync.finished_at) await refresh();
    }
  }

  let scoutTimer = null;
  async function pollScout() {
    const scout = await getJSON("/api/scout");
    updateScoutButton(scout);
    if (scout.running) scoutTimer = setTimeout(pollScout, 3000);
    else {
      scoutTimer = null;
      if (scout.finished_at) await refresh();
    }
  }

  async function refresh() {
    const data = await loadOverview();
    if (data && data.setup && !data.setup.configured) return;
    await Promise.all([loadPipeline(), loadMail()]);
  }

  $("btnSync").addEventListener("click", async () => {
    try {
      const sync = await postJSON("/api/sync", {});
      updateSyncButton(sync);
      if (!pollTimer) pollSync();
    } catch (err) {
      setSyncNote(err.message, true);
    }
  });

  $("btnScout").addEventListener("click", async () => {
    try {
      const scout = await postJSON("/api/scout", {});
      updateScoutButton(scout);
      if (!scoutTimer) pollScout();
    } catch (err) {
      setSyncNote(err.message, true);
    }
  });

  $("btnSetupCancel").addEventListener("click", () => {
    location.hash = "#/";
  });
  window.addEventListener("hashchange", () => {
    applyRoute().catch((err) => setSyncNote(err.message, true));
  });
  $("setupForm").addEventListener("submit", saveSetup);
  $("scraperType").addEventListener("change", toggleAdzuna);

  $("statusFilter").addEventListener("change", loadPipeline);
  let searchTimer;
  $("jobSearch").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadPipeline, 250);
  });
  $("mailStage").addEventListener("change", loadMail);
  $("mailLinked").addEventListener("change", loadMail);

  refresh()
    .then(() => applyRoute())
    .catch((err) => setSyncNote(err.message, true));
})();
