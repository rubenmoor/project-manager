// What a task's commit did to tasks.sql, one line per row it touched: tasks created first, then tasks, plans, decisions
// and milestones changed. The task itself is left out; its outcome card shows it.
import { html } from "/vendor/preact-htm.js";
import { Named, Kind } from "/ui.js";

const ORDER = ["task", "plan", "decision", "milestone"];
const QUIET = new Set(["status", "retired_at", "closed_at", "implemented_at", "note", "answer", "reason", "consequences"]);
const RANK = { created: 0, asked: 1, "sign-off": 1, recorded: 1 };

function added(k, r) {
  if (k !== "decision") return "created";
  return r.signoff ? "sign-off" : r.decided_by_task != null ? "recorded" : r.status === "pending" ? "asked" : r.status;
}

function changed(k, row, before) {
  const s = before.status;
  if (s === undefined) return k === "decision" && Object.keys(before).every((f) => f === "consequences") ? "consequences" : "edited";
  if (row.status === "open" || row.status === "pending") return "reopened";
  return row.status;
}

const edits = (before) => Object.keys(before).filter((f) => !QUIET.has(f));

export function effects(rows, self) {
  if (!rows) return [];
  const out = [];
  const find = (k, id) => (rows[k] && [...rows[k].added, ...rows[k].changed.map((c) => c.row)].find((r) => r.id === id)) || null;
  for (const k of ORDER) {
    const x = rows[k];
    if (!x) continue;
    for (const r of x.added) out.push({ k, r, how: added(k, r) });
    for (const c of x.changed) if (!(k === "task" && c.row.id === self)) out.push({ k, r: c.row, before: c.before, how: changed(k, c.row, c.before) });
    for (const r of x.removed) out.push({ k, r, how: "deleted" });
  }
  const link = (t, id, how, other) => out.push({ k: t, r: find(t, id) || { id }, how, other });
  for (const l of (rows.task_decision || {}).added || []) link("task", l.task_id, "waits", { k: "decision", id: l.decision_id });
  for (const l of (rows.task_decision || {}).removed || []) link("task", l.task_id, "unblocked", { k: "decision", id: l.decision_id });
  for (const l of (rows.decision_supersedes || {}).added || []) link("decision", l.decision_id, "supersedes", { k: "decision", id: l.superseded_id });
  return out.sort((a, b) => ORDER.indexOf(a.k) - ORDER.indexOf(b.k) || (RANK[a.how] ?? 2) - (RANK[b.how] ?? 2));
}

const LABEL = { created: "new", waits: "waits for", unblocked: "no longer waits for", "sign-off": "sign-off asked" };

function Detail({ d, e }) {
  const { k, r, before, how, other } = e;
  if (other) {
    const o = d[other.k].get(other.id) || { id: other.id, subject: "" };
    return html`<span class="muted"><a href=${`#/${other.k}/${o.id}`}><${Named} kind=${other.k} r=${o} /></a></span>`;
  }
  if (how === "created" && k === "task") return html`<${Kind} kind=${r.kind} />`;
  if (how === "dropped" && r.note) return html`<span class="muted">${r.note}</span>`;
  if (how === "answered" || how === "recorded") return html`<span class="muted">${r.answer}</span>`;
  if (how === "irrelevant") return html`<span class="muted">${r.reason}</span>`;
  const f = before ? edits(before) : [];
  if (!f.length) return null;
  return html`<span class="muted">${how === "edited" ? "" : "also "}${f.join(", ")}${before.subject ? html` (was <del>${before.subject}</del>)` : null}</span>`;
}

export function Affected({ d, t, rows }) {
  const list = effects(rows, t.id);
  if (!list.length) return null;
  const made = list.filter((e) => e.k === "task" && e.how === "created").length;
  return html`<h3>Affected${made ? html` <span class="muted">· ${made} task${made > 1 ? "s" : ""} created</span>` : null}</h3>
    <ul class="plain affected">${list.map((e) => {
      const live = d[e.k].get(e.r.id);
      const r = e.r.subject ? e.r : live || { id: e.r.id, subject: "" };
      return html`<li class=${`fx-${e.how}`}><span class="fx">${LABEL[e.how] || e.how}</span>
        <span class="fx-what">${e.how === "deleted" || !live ? html`<${Named} kind=${e.k} r=${r} />`
          : html`<a href=${`#/${e.k}/${r.id}`}><${Named} kind=${e.k} r=${r} /></a>`}
        <${Detail} d=${d} e=${e} /></span></li>`;
    })}</ul>`;
}
