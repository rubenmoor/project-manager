// Reports: executive summaries (each reports the delta since the last) and resource use reports (where time, tokens
// and cost went, by categories the worker made from all done tasks). A button queues the exec task that writes each.
import { html, useState } from "/vendor/preact-htm.js";
import { Markdown, Time, TaskLink, Fold, Bars } from "/ui.js";
import { stamp, usd, tokens, duration } from "/data.js";
import { post } from "/editor.js";

// While an exec task writes one (pm's open_summary, open_report), its button is disabled and links to it.
function Request({ d, op, open, label, queued, writing }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const go = () => {
    setBusy(true); setError(null);
    post({ op }).catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  const t = open != null ? d.task.get(open) : null;
  const running = t && d.running && d.running.task_id === t.id;
  const pos = t ? d.queuePos.get(t.id) : null;
  return html`<div class="row"><button class="btn primary" onClick=${go} disabled=${busy || !!t}>
      ${t ? (running ? writing : queued) : label}</button>
    ${t ? html`<span class="sub"><${TaskLink} t=${t} />${running ? html` · <a href=${`#/task/${t.id}?log`}>live log</a>`
        : pos != null ? `, #${pos + 1} in the queue` : ", not in the queue"}</span>`
    : html`<span class="muted">queues an exec task at priority 10000</span>`}
    ${error ? html`<span class="err">${error}</span>` : null}</div>`;
}

const head = (kind, s) => html`<span class="muted">${kind} ${s.id} · ${stamp(s.created_at)} (<${Time} ts=${s.created_at} />) ·
  <a href=${`#/task/${s.task_id}`}>task ${s.task_id}</a>${s.head_commit ? html` · <span class="mono">${s.head_commit.slice(0, 7)}</span>` : null}</span>`;

function Report({ r }) {
  const cats = [...r.categories].sort((a, b) => b.seconds - a.seconds);
  const tot = (k) => cats.reduce((s, c) => s + c[k], 0);
  const T = { tasks: tot("tasks"), seconds: tot("seconds"), tokens: tot("tokens"), cost: tot("cost") };
  const pct = (v, k) => (T[k] ? `${Math.round((100 * v) / T[k])}%` : "");
  const row = (c, b) => html`<tr>
    <td>${b ? html`<b>${c.name}</b>` : html`<b>${c.name}</b><div class="muted">${c.criteria}</div>`}</td>
    <td class="r num">${c.tasks}</td><td class="r num">${duration(c.seconds)}</td><td class="r num muted">${pct(c.seconds, "seconds")}</td>
    <td class="r num">${tokens(c.tokens) || "0"}</td><td class="r num">${usd(c.cost)}</td><td class="r num muted">${pct(c.cost, "cost")}</td></tr>`;
  return html`<div class="card">${head("report", r)}
    <div style="margin:12px 0"><${Bars} horizontal data=${cats.map((c) => ({ label: c.name, value: c.seconds / 3600 }))}
      fmt=${(h) => `${h.toFixed(1)} h`} /></div>
    <div class="tablewrap"><table><thead><tr><th>category</th><th class="r">tasks</th><th class="r">time</th><th></th>
      <th class="r">tokens</th><th class="r">cost</th><th></th></tr></thead>
      <tbody>${cats.map((c) => row(c))}${row({ name: "Total", ...T }, true)}</tbody></table></div></div>`;
}

export function Reports({ d }) {
  const summaries = [...d.summaries].sort((a, b) => b.id - a.id);
  const reports = [...d.reports].sort((a, b) => b.id - a.id);
  const [latest, ...older] = summaries;
  const [report, ...earlier] = reports;
  return html`<div class="section-head"><h1>Reports</h1></div>
    <h2>Executive summary</h2>
    <div class="sub">Written by a worker from the project's state; each one reports what changed since the one before.</div>
    <div style="margin:12px 0"><${Request} d=${d} op="summary_request" open=${d.state.open_summary}
      label="Write an executive summary" queued="Executive summary queued" writing="Writing an executive summary…" /></div>
    ${!latest ? html`<div class="empty">No summary yet.</div>` : html`<div class="card">${head("summary", latest)}<${Markdown} text=${latest.body} /></div>`}
    ${older.length ? html`<${Fold} head=${html`<h3>Earlier summaries (${older.length})</h3>`}>
      ${older.map((s) => html`<div class="card" style="margin-top:8px">${head("summary", s)}<${Markdown} text=${s.body} /></div>`)}</${Fold}>` : null}
    <h2 style="margin-top:28px">Resource use</h2>
    <div class="sub">A worker sorts every done task into categories it draws up from scratch; time, tokens and cost are the tasks' runs.</div>
    <div style="margin:12px 0"><${Request} d=${d} op="report_request" open=${d.state.open_report}
      label="Write a resource use report" queued="Resource use report queued" writing="Writing a resource use report…" /></div>
    ${!report ? html`<div class="empty">No report yet.</div>` : html`<${Report} r=${report} />`}
    ${earlier.length ? html`<${Fold} head=${html`<h3>Earlier reports (${earlier.length})</h3>`}>
      ${earlier.map((r) => html`<div style="margin-top:8px"><${Report} r=${r} /></div>`)}</${Fold}>` : null}`;
}
