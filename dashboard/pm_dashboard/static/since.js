// "Since you last looked": rows changed between the commit you marked as seen and now.
import { html, useState, useEffect } from "/vendor/preact-htm.js";
import { Pill, Time } from "/ui.js";
import { usd, stamp } from "/data.js";

const KEY = "pm-seen";
export const getSeen = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
export const setSeen = (c) => { try { localStorage.setItem(KEY, c); } catch {} };

// Rows changed since the seen commit; refetched when the state changes. The first visit marks the current HEAD.
export function useSince(state) {
  const [since, setSince] = useState(null);
  const [seen, setSeenState] = useState(getSeen());
  const head = state && state.head && state.head.commit;
  useEffect(() => { if (!seen && head) { setSeen(head); setSeenState(head); } }, [head]);
  useEffect(() => {
    if (!seen) return;
    let gone = false;
    fetch(`/api/since?commit=${seen}`).then((r) => r.json()).then((j) => !gone && setSince(j)).catch(() => {});
    return () => { gone = true; };
  }, [seen, state && state.read_at]);
  const mark = () => { if (head) { setSeen(head); setSeenState(head); } };
  return { since, mark, seen };
}

export function sinceCount(since) {
  if (!since || !since.tables) return 0;
  return Object.entries(since.tables).filter(([t]) => t !== "run" && t !== "task_decision")
    .reduce((n, [, x]) => n + x.added.length + x.removed.length + x.changed.length, 0);
}

// Line diff (LCS) of two texts: [[" " | "+" | "-", line]].
export function lineDiff(a, b) {
  const x = (a || "").split("\n"), y = (b || "").split("\n");
  const n = x.length, m = y.length;
  const L = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push([" ", x[i]]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push(["-", x[i++]]);
    else out.push(["+", y[j++]]);
  }
  while (i < n) out.push(["-", x[i++]]);
  while (j < m) out.push(["+", y[j++]]);
  return out;
}

export function TextDiff({ before, after }) {
  const d = lineDiff(before, after);
  const keep = new Set();
  d.forEach(([k], i) => { if (k !== " ") for (let j = i - 2; j <= i + 2; j++) keep.add(j); });
  let gap = false;
  return html`<div class="tdiff">${d.map(([k, l], i) => {
    if (!keep.has(i)) { const g = !gap; gap = true; return g ? html`<div class="gap">⋯</div>` : null; }
    gap = false;
    return html`<div class=${k === "+" ? "add" : k === "-" ? "del" : ""}><span class="sign">${k}</span>${l || " "}</div>`;
  })}</div>`;
}

const LABEL = { milestone: "Milestones", plan: "Plans", task: "Tasks", decision: "Decisions" };
const TEXT = new Set(["body", "note"]);
const SKIP = new Set(["retired_at", "closed_at", "implemented_at"]);

function Change({ table, row, before }) {
  const fields = Object.keys(before).filter((f) => !SKIP.has(f));
  return html`<div class="change">
    <div><a href=${`#/${table}/${row.id}`}>${row.subject}</a> <span class="muted">${table} ${row.id}</span></div>
    ${fields.map((f) => TEXT.has(f) && (before[f] || "").includes("\n") || TEXT.has(f) && (row[f] || "").includes("\n")
      ? html`<div class="field"><span class="muted">${f}</span><${TextDiff} before=${before[f]} after=${row[f]} /></div>`
      : html`<div class="field"><span class="muted">${f}</span> ${f === "status" ? html`<${Pill} status=${before[f]} /> → <${Pill} status=${row[f]} />`
        : html`<del>${fmt(before[f])}</del> → <ins>${fmt(row[f])}</ins>`}</div>`)}
  </div>`;
}

const fmt = (v) => (v == null ? "none" : Array.isArray(v) ? v.join(" / ") : String(v));

export function Since({ since, mark, state }) {
  if (!since) return html`<div class="empty">Loading…</div>`;
  if (since.error) return html`<h1>Since you last looked</h1><div class="banner error">${since.error}</div>
    <button class="btn" onClick=${mark}>Start over from now</button>`;
  const t = since.tables;
  const runs = t.run ? t.run.added.filter((r) => r.result !== "running") : [];
  const closedDecisions = t.decision ? t.decision.changed.filter((c) => c.before.status === "pending" && c.row.status !== "pending") : [];
  const n = sinceCount(since);
  const atHead = state.head && since.commit === state.head.commit;
  return html`<div class="section-head"><h1>Since you last looked</h1>
      <button class="btn" onClick=${mark} disabled=${atHead && !state.dirty.length}>Mark all as seen</button></div>
    <div class="sub">Changes since <span class="mono">${since.commit.slice(0, 7)}</span> (${stamp(since.date)}, <${Time} ts=${since.date} />),
      uncommitted ones included.</div>
    ${!n && !runs.length ? html`<div class="empty" style="margin-top:12px">Nothing changed.</div>` : null}
    ${closedDecisions.length ? html`<h2>Decisions answered (${closedDecisions.length})</h2>
      <div class="tablewrap"><table><tbody>${closedDecisions.map((c) => html`<tr><td class="subject"><a href=${`#/decision/${c.row.id}`}>${c.row.subject}</a></td>
        <td><${Pill} status=${c.row.status} /></td><td>${c.row.answer || c.row.reason}</td></tr>`)}</tbody></table></div>` : null}
    ${["milestone", "plan", "task", "decision"].filter((k) => t[k]).map((k) => {
      const x = t[k];
      const changed = k === "decision" ? x.changed.filter((c) => !closedDecisions.includes(c)) : x.changed;
      if (!x.added.length && !changed.length && !x.removed.length) return null;
      return html`<h2>${LABEL[k]}</h2>
        ${x.added.length ? html`<div class="card"><b>New (${x.added.length})</b><ul class="plain">${x.added.map((r) => html`<li>
          <a href=${`#/${k}/${r.id}`}>${r.subject}</a> <span class="muted">${k} ${r.id}</span>
          ${r.status && !["open", "pending"].includes(r.status) ? html` <${Pill} status=${r.status} />` : null}
          ${r.created_by_task != null ? html` <span class="muted">by task ${r.created_by_task}</span>` : null}</li>`)}</ul></div>` : null}
        ${changed.length ? html`<div class="card" style="margin-top:8px"><b>Changed (${changed.length})</b>
          ${changed.map((c) => html`<${Change} table=${k} row=${c.row} before=${c.before} />`)}</div>` : null}
        ${x.removed.length ? html`<div class="card" style="margin-top:8px"><b>Removed (${x.removed.length})</b>
          <ul class="plain">${x.removed.map((r) => html`<li>${r.subject} <span class="muted">${k} ${r.id}</span></li>`)}</ul></div>` : null}`;
    })}
    ${runs.length ? html`<h2>Runs</h2><div class="sub">${`${runs.length} runs, ${usd(runs.reduce((s, r) => s + (r.cost_usd || 0), 0))}, ${runs.filter((r) => r.result === "failed").length} failed. `}<a href="#/runs">All runs</a></div>` : null}`;
}
