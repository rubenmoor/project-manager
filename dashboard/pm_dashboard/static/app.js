import { html, render, useState, useEffect, useMemo, useRef } from "/vendor/preact-htm.js";
import { index } from "/data.js";
import { Pill, Elapsed, Time } from "/ui.js";
import { Overview, Queue, Milestones, Milestone, Plan, Task, Decisions, Decision, Runs } from "/views.js";
import { Live } from "/live.js";
import { MilestoneAdded } from "/add.js";
import { Since, useSince, sinceCount } from "/since.js";
import { useNotifications, NotifyButton } from "/notify.js";
import { UsageLine } from "/usage.js";

function route() {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  return { page: parts[0] || "home", id: parts[1] ? Number(parts[1]) : null, query: query || null };
}

// /api/state, refetched whenever the server's event stream says something changed.
function useState2() {
  const [state, setState] = useState(null);
  const [log, setLog] = useState(null);
  const [online, setOnline] = useState(true);
  useEffect(() => {
    let version = null, inflight = false, again = false;
    const load = () => {
      if (inflight) { again = true; return; }
      inflight = true;
      fetch("/api/state").then((r) => r.json()).then(setState).catch(() => setOnline(false)).finally(() => {
        inflight = false;
        if (again) { again = false; load(); }
      });
    };
    load();
    const es = new EventSource("/api/events");
    es.addEventListener("state", (e) => {
      const m = JSON.parse(e.data);
      setOnline(true);
      setLog(m.log);
      if (m.version !== version) { version = m.version; load(); }
    });
    es.onerror = () => setOnline(false);
    es.onopen = () => setOnline(true);
    return () => es.close();
  }, []);
  return { state, log, online };
}

const NAV = [["live", "Live", "l"], ["overview", "Overview", "o"], ["queue", "Queue", "q"], ["milestones", "Milestones", "m"],
  ["decisions", "Decisions", "d"], ["since", "Since you looked", "s"], ["runs", "Runs", "r"]];
const SECTION = { added: "milestones", milestone: "milestones", plan: "milestones", task: "milestones", decision: "decisions" };

function Search({ d }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef(null);
  useEffect(() => {
    const k = (e) => {
      if (e.key === "/" && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); input.current.focus(); }
    };
    addEventListener("keydown", k);
    return () => removeEventListener("keydown", k);
  }, []);
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
    <input ref=${input} placeholder="Search  /" value=${q} onInput=${(e) => { setQ(e.target.value); setSel(0); }} onKeyDown=${key} />
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
  const { state, log, online } = useState2();
  const [r0, setR] = useState(route());
  let r = r0;
  useEffect(() => {
    const f = () => { setR(route()); scrollTo(0, 0); };
    addEventListener("hashchange", f);
    let g = false;
    const k = (e) => {
      if (/INPUT|TEXTAREA/.test(document.activeElement.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "g") { g = true; setTimeout(() => (g = false), 800); return; }
      const n = g && NAV.find((x) => x[2] === e.key);
      if (n) location.hash = `#/${n[0]}`;
      g = false;
    };
    addEventListener("keydown", k);
    return () => { removeEventListener("hashchange", f); removeEventListener("keydown", k); };
  }, []);
  const shown = state && (state.tables ? state : state.last_good);
  const d = useMemo(() => (shown ? index({ ...shown, __log: log }) : null), [shown, log]);
  const since = useSince(shown);
  useNotifications(d);
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
  const nSince = sinceCount(since.since);
  const View = { added: MilestoneAdded, live: Live, since: (p) => html`<${Since} since=${since.since} mark=${since.mark} state=${p.d.state} />`, overview: Overview, queue: Queue, milestones: Milestones, milestone: Milestone, plan: Plan, task: Task,
    decisions: Decisions, decision: Decision, runs: Runs }[r.page];
  const root = state ? state.root.split("/").pop() : "";
  return html`<div class="app">
    <header class="top">
      <span class="conn ${online ? "" : "off"}" title=${online ? "live" : "disconnected from pm-dashboard"}></span>
      <span class="brand">pm<small>${root}</small></span>
      ${d && d.running ? html`<a class="running" href=${`#/task/${d.running.task_id}?log`}><${Pill} status="running" label=${`task ${d.running.task_id}`} />
        <${Elapsed} start=${d.running.started_at} /></a>` : null}
      <span class="grow head">${state && state.head ? html`<span class="mono">${state.head.commit.slice(0, 7)}</span> ${state.head.subject}
        <span class="muted"> · <${Time} ts=${state.head.date} /></span>` : null}
        ${state && state.dirty.length ? html` <span class="tag" title=${state.dirty.join("\n")}>${state.dirty.length} uncommitted</span>` : null}</span>
      <${Search} d=${d} />
      <${NotifyButton} />
      <${Theme} />
    </header>
    <nav class="nav">${NAV.map(([k, label]) => html`<a href=${`#/${k}`} class=${page === k ? "on" : ""}>
      <span>${label}</span>${k === "decisions" && pending ? html`<span class="tag">${pending}</span>` : null}
      ${k === "since" && nSince ? html`<span class="tag new">${nSince}</span>` : null}
      ${k === "live" && d && d.running ? html`<span class="livedot" title="a worker is running"></span>` : null}</a>`)}
      <div class="keys">g + l o q m d s r: go to<br />/ search · i edit · Esc save</div></nav>
    <main class=${"main" + (page === "live" ? " full" : "")}>
      ${state && state.error ? html`<div class="banner error"><b>Can't read the database.</b> <pre>${state.error}</pre>
        ${state.last_good ? html`<div class="muted">Showing the last state that could be read.</div>` : null}</div>` : null}
      ${!state ? html`<div class="empty">Loading…</div>` : d && View ? html`<${View} d=${d} id=${r.id} query=${r.query} />`
        : d ? html`<div class="empty">No such page.</div>` : null}
    </main>
    <${UsageLine} d=${d} version=${shown} />
  </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
