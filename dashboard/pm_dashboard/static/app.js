import { html, render, useState, useEffect, useMemo, useRef } from "/vendor/preact-htm.js";
import { index } from "/data.js";
import { Pill, Elapsed, Time, Worktree } from "/ui.js";
import { Overview, Queue, Milestones, Milestone, Plan, Task, Decisions, Decision, Runs } from "/views.js";
import { Live } from "/live.js";
import { MilestoneAdded } from "/add.js";
import { Reports } from "/reports.js";
import { Tasks } from "/tasks.js";
import { useNotifications, NotifyButton } from "/notify.js";
import { UsageLine } from "/usage.js";
import { Celebrate } from "/celebrate.js";
import { PROJECT, Switcher, NoProject } from "/projects.js";

function route() {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  return { page: parts[0] || "home", id: parts[1] ? Number(parts[1]) : null, query: query || null };
}

// A restarted server with another pm version (pm-dashboard-live): reload, once no editor has focus.
function reloadWhenIdle() {
  const busy = () => document.activeElement && document.activeElement.closest("input, textarea, select, .cm-editor");
  if (!busy()) return location.reload();
  setTimeout(reloadWhenIdle, 1000);
}

// The project's api/state, refetched whenever polling says it changed; every project's summary.
function useState2() {
  const [state, setState] = useState(null);
  const [log, setLog] = useState(null);
  const [projects, setProjects] = useState(null);
  const [online, setOnline] = useState(true);
  useEffect(() => {
    let version = null, pmVersion = null, inflight = false, again = false;
    const load = () => {
      if (!PROJECT) return;
      if (inflight) { again = true; return; }
      inflight = true;
      fetch("api/state").then((r) => r.json()).then(setState).catch(() => setOnline(false)).finally(() => {
        inflight = false;
        if (again) { again = false; load(); }
      });
    };
    load();
    // Polled, not a held connection: browsers allow ~6 per host, and every open tab would hold one.
    let gone = false, timer = null, polling = false, seenProjects = null, seenLog = null;
    const poll = () => {
      polling = true;
      fetch(`/api/poll?project=${encodeURIComponent(PROJECT || "")}`).then((r) => r.json()).then((m) => {
        if (gone) return;
        setOnline(true);
        if (pmVersion !== null && m.pm !== pmVersion) return reloadWhenIdle();
        pmVersion = m.pm;
        const ps = JSON.stringify(m.projects);
        if (ps !== seenProjects) { seenProjects = ps; setProjects(m.projects); }
        if (PROJECT && !m.projects.some((p) => p.name === PROJECT)) location.href = "/";   // removed
        const lg = JSON.stringify(m.log);
        if (lg !== seenLog) { seenLog = lg; setLog(m.log); }
        if (m.version !== undefined && m.version !== version) { version = m.version; load(); }
      }).catch(() => !gone && setOnline(false)).finally(() => {
        polling = false;
        if (!gone) timer = setTimeout(poll, document.hidden ? 5000 : 1000);
      });
    };
    poll();
    const wake = () => { if (!document.hidden && !polling) { clearTimeout(timer); poll(); } };
    document.addEventListener("visibilitychange", wake);
    return () => { gone = true; clearTimeout(timer); document.removeEventListener("visibilitychange", wake); };
  }, []);
  return { state, log, projects, online };
}

const NAV = [["live", "Live"], ["overview", "Overview"], ["queue", "Queue"], ["tasks", "Tasks"], ["milestones", "Milestones"],
  ["decisions", "Decisions"], ["reports", "Reports"], ["runs", "Runs"]];
const SECTION = { summary: "reports", added: "milestones", milestone: "milestones", plan: "milestones", task: "tasks", decision: "decisions" };

function Search({ d }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef(null);
  const hits = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s || !d) return [];
    const num = /^\d+$/.test(s) ? Number(s) : null;
    const all = [...d.tasks.map((r) => ["task", r]), ...d.plans.map((r) => ["plan", r]),
      ...d.milestones.map((r) => ["milestone", r]), ...d.decisions.map((r) => ["decision", r])];
    return all.filter(([, r]) => r.id === num || r.subject.toLowerCase().includes(s)
      || (s.length > 2 && (r.body || "").toLowerCase().includes(s)))
      .sort((a, b) => (b[1].id === num) - (a[1].id === num)).slice(0, 12);
  }, [q, d]);
  const go = (h) => { location.hash = `#/${h[0]}/${h[1].id}`; setQ(""); input.current.blur(); };
  const key = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setSel(Math.min(sel + 1, hits.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel(Math.max(sel - 1, 0)); }
    else if (e.key === "Enter" && hits[sel]) go(hits[sel]);
    else if (e.key === "Escape") { setQ(""); input.current.blur(); }
  };
  return html`<div class="search">
    <input ref=${input} placeholder="Search" value=${q} onInput=${(e) => { setQ(e.target.value); setSel(0); }} onKeyDown=${key} />
    ${hits.length ? html`<div class="results">${hits.map((h, i) => html`<a href=${`#/${h[0]}/${h[1].id}`} class=${i === sel ? "sel" : ""}
      onMouseDown=${(e) => { e.preventDefault(); go(h); }}><span class="tag">${h[0]} ${h[1].id}</span><span>${h[1].subject}</span></a>`)}</div>` : null}
  </div>`;
}

function Theme() {
  const get = () => { try { return localStorage.getItem("pm-theme"); } catch { return null; } };
  const [theme, setTheme] = useState(get());
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
    try { theme ? localStorage.setItem("pm-theme", theme) : localStorage.removeItem("pm-theme"); } catch {}
  }, [theme]);
  const next = { null: "light", light: "dark", dark: null }[theme];
  return html`<button class="iconbtn" title="theme: light, dark or the system's" onClick=${() => setTheme(next)}>${theme || "auto"}</button>`;
}

function App() {
  const { state, log, projects, online } = useState2();
  const [r0, setR] = useState(route());
  let r = r0;
  useEffect(() => {
    const f = () => { setR(route()); scrollTo(0, 0); };
    addEventListener("hashchange", f);
    return () => removeEventListener("hashchange", f);
  }, []);
  const shown = state && (state.tables ? state : state.last_good);
  const d = useMemo(() => (shown ? index({ ...shown, __log: log }) : null), [shown, log]);
  useNotifications(projects);
  // The home page: the run view while pm runs (or ran in the last 5 minutes), else the overview; chosen on arrival.
  const [home, setHome] = useState(null);
  useEffect(() => {
    if (r0.page !== "home") { setHome(null); return; }
    if (!d || home) return;
    const recent = d.runs.some((x) => x.ended_at && Date.now() - new Date(x.ended_at) < 300e3);
    setHome(d.milestones.length && (d.running || recent) ? "live" : "overview");
  }, [r0.page, !!d]);
  if (r.page === "home") r = { ...r, page: home || "overview" };
  const page = SECTION[r.page] || r.page;
  const pending = d ? d.decisions.filter((x) => x.status === "pending").length : 0;
  const View = { added: MilestoneAdded, live: Live, reports: Reports, summary: Reports, tasks: Tasks, overview: Overview, queue: Queue, milestones: Milestones, milestone: Milestone, plan: Plan, task: Task,
    decisions: Decisions, decision: Decision, runs: Runs }[r.page];
  const root = PROJECT || "";
  useEffect(() => { document.title = root ? `${root} - PM Dashboard` : "PM Dashboard"; }, [root]);
  if (!PROJECT) return html`<div class="app"><header class="top">
      <span class="conn ${online ? "" : "off"}"></span><span class="brand">pm</span><span class="grow"></span><${Theme} /></header>
    <main class="main"><${NoProject} /></main></div>`;
  return html`<div class="app">
    <header class="top">
      <span class="conn ${online ? "" : "off"}" title=${online ? "live" : "disconnected from pm-dashboard"}></span>
      <${Switcher} projects=${projects} current=${root} />
      ${d ? d.runningAll.map((r) => html`<a class="running" href=${`#/task/${r.task_id}?log`}><${Pill} status="running" label=${`task ${r.task_id}`} />
        ${r.worktree ? html`<${Worktree} name=${r.worktree} /> ` : null}<${Elapsed} start=${r.started_at} /></a>`) : null}
      <span class="grow head">${state && state.head ? html`<span class="mono">${state.head.commit.slice(0, 7)}</span> ${state.head.subject}
        <span class="muted"> · <${Time} ts=${state.head.date} /></span>` : null}
        ${state && state.dirty && state.dirty.length ? html` <span class="tag" title=${state.dirty.join("\n")}>${state.dirty.length} uncommitted</span>` : null}</span>
      <${Search} d=${d} />
      <${NotifyButton} />
      <${Theme} />
    </header>
    <nav class="nav">${NAV.map(([k, label]) => html`<a href=${`#/${k}`} class=${page === k ? "on" : ""}>
      <span>${label}</span>${k === "decisions" && pending ? html`<span class="tag">${pending}</span>` : null}
      ${k === "live" && d && d.running ? html`<span class="livedot" title="a worker is running"></span>` : null}</a>`)}</nav>
    <main class=${"main" + (page === "live" ? " full" : "")}>
      ${state && state.error ? html`<div class="banner error"><b>Can't read the database.</b> <pre>${state.error}</pre>
        ${state.last_good ? html`<div class="muted">Showing the last state that could be read.</div>` : null}</div>` : null}
      ${d ? html`<${TrunkDirty} d=${d} />` : null}
      ${!state ? html`<div class="empty">Loading…</div>` : d && View ? html`<${View} d=${d} id=${r.id} query=${r.query} />`
        : d ? html`<div class="empty">No such page.</div>` : null}
    </main>
    <${UsageLine} d=${d} version=${shown} />
    <${Celebrate} d=${d} />
  </div>`;
}

// Uncommitted changes in the trunk while no trunk task runs: the trunk's tasks, and the ones they plan for, wait.
function TrunkDirty({ d }) {
  if (!d.trunkBlocked) return null;
  const h = d.trunkHeld, waiting = h ? [...d.holds].filter(([, p]) => p === h.id).map(([id]) => id) : [];
  const n = d.trunkDirty.length;
  return html`<div class="banner warn trunk-dirty"><b>${n} uncommitted change${n > 1 ? "s" : ""} in the trunk</b>
    <span class="muted mono" title=${d.trunkDirty.join("\n")}> ${d.trunkDirty.slice(0, 3).join(", ")}${n > 3 ? ", …" : ""}</span>.
    ${h ? html` The trunk's next task, <a href=${`#/task/${h.id}`}>task ${h.id}</a>, can't start${waiting.length
      ? html`, nor the ${waiting.length} task${waiting.length > 1 ? "s" : ""} it may rewrite` : ""}. ` : " Trunk tasks wait for them. "}Commit or stash them.</div>`;
}

render(html`<${App} />`, document.getElementById("app"));
