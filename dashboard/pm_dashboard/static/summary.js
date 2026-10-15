// Executive summaries: a button queues the task that writes one (pm summary request); it reports the delta since the last.
import { html, useState } from "/vendor/preact-htm.js";
import { Markdown, Pill, Time, TaskLink, Fold } from "/ui.js";
import { stamp } from "/data.js";
import { post } from "/editor.js";

// The open summary task, if any: pm's SUMMARY_TASK.
export const isSummary = (t) => t.plan_id == null && t.subject === "write an executive summary";
export const openSummary = (d) => d.tasks.find((t) => isSummary(t) && t.status === "open") || null;

function Request({ d }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const open = openSummary(d);
  const go = () => {
    setBusy(true); setError(null);
    post({ op: "summary_request" }).catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  if (open) {
    const running = d.running && d.running.task_id === open.id;
    const pos = d.queuePos.get(open.id);
    return html`<div class="card"><div class="row"><${Pill} status=${running ? "running" : "open"} label=${running ? "writing" : "queued"} />
      <span class="sub"><${TaskLink} t=${open} />${running ? html` · <a href=${`#/task/${open.id}?log`}>live log</a>`
        : pos === 0 ? (d.running ? ", next after the running task" : html`, next; start it with <code>pm run</code>`)
        : pos != null ? `, #${pos + 1} in the queue` : ""}</span></div></div>`;
  }
  return html`<div class="row"><button class="btn primary" onClick=${go} disabled=${busy}>Write an executive summary</button>
    <span class="muted">queues a task at priority 10000: <code>pm run</code> works it next</span>
    ${error ? html`<span class="err">${error}</span>` : null}</div>`;
}

export function Summary({ d }) {
  const all = [...d.summaries].sort((a, b) => b.id - a.id);
  const [latest, ...older] = all;
  const head = (s) => html`<span class="muted">summary ${s.id} · ${stamp(s.created_at)} (<${Time} ts=${s.created_at} />) ·
    <a href=${`#/task/${s.task_id}`}>task ${s.task_id}</a>${s.head_commit ? html` · <span class="mono">${s.head_commit.slice(0, 7)}</span>` : null}</span>`;
  return html`<div class="section-head"><h1>Executive summary</h1></div>
    <div class="sub">Written by a worker from the project's state; each one reports what changed since the one before.</div>
    <div style="margin:12px 0"><${Request} d=${d} /></div>
    ${!latest ? html`<div class="empty">No summary yet.</div>` : html`<div class="card">${head(latest)}<${Markdown} text=${latest.body} /></div>`}
    ${older.length ? html`<${Fold} head=${html`<h2>Earlier (${older.length})</h2>`}>
      ${older.map((s) => html`<div class="card" style="margin-top:8px">${head(s)}<${Markdown} text=${s.body} /></div>`)}</${Fold}>` : null}`;
}
