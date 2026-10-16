// Reports: executive summaries (each reports the delta since the last), resource use reports (where time, tokens
// and cost went, by categories the worker made from all done tasks) and custom reports (the human's question, answered
// in one paragraph). A button or the form queues the exec task that writes each.
import { html, useState } from "/vendor/preact-htm.js";
import { Markdown, Time, TaskLink, Bars } from "/ui.js";
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
  const running = t && d.isRunning(t.id);
  const pos = t ? d.queuePos.get(t.id) : null;
  return html`<div class="row"><button class="btn primary" onClick=${go} disabled=${busy || !!t}>
      ${t ? (running ? writing : queued) : label}</button>
    ${t ? html`<span class="sub"><${TaskLink} t=${t} />${running ? html` · <a href=${`#/task/${t.id}?log`}>live log</a>`
        : pos != null ? `, #${pos + 1} in the queue` : ", not in the queue"}</span>`
    : html`<span class="muted">queues an exec task at priority 10000</span>`}
    ${error ? html`<span class="err">${error}</span>` : null}</div>`;
}

// The human's question or request; pm report ask queues an exec task that answers it in one paragraph.
function Ask({ d }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const go = () => {
    if (!q.trim()) return;
    setBusy(true); setError(null);
    post({ op: "report_ask", question: q }).then(() => setQ("")).catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  const key = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) go(); };
  const open = d.customs.filter((c) => c.answer == null).map((c) => [c, d.task.get(c.task_id)]).filter(([, t]) => t && t.status === "open");
  return html`<div class="card ask"><b>Custom one-paragraph report</b>
    <textarea rows="3" placeholder="Ask a question or request a report" value=${q} onInput=${(e) => setQ(e.target.value)} onKeyDown=${key}></textarea>
    <div class="row"><button class="btn primary" onClick=${go} disabled=${busy || !q.trim()}>Ask</button>
      <span class="muted">queues an exec task at priority 10000 · Ctrl+Enter</span>${error ? html`<span class="err">${error}</span>` : null}</div>
    ${open.map(([c, t]) => {
      const running = d.isRunning(t.id), pos = d.queuePos.get(t.id);
      return html`<div class="sub"><${TaskLink} t=${t} />${running ? html` · <a href=${`#/task/${t.id}?log`}>answering…</a>`
        : pos != null ? `, #${pos + 1} in the queue` : ", not in the queue"}</div>`;
    })}</div>`;
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
  return html`<div class="card">${head("resource use report", r)}
    <div style="margin:12px 0"><${Bars} horizontal data=${cats.map((c) => ({ label: c.name, value: c.seconds / 3600 }))}
      fmt=${(h) => `${h.toFixed(1)} h`} /></div>
    <div class="tablewrap"><table><thead><tr><th>category</th><th class="r">tasks</th><th class="r">time</th><th></th>
      <th class="r">tokens</th><th class="r">cost</th><th></th></tr></thead>
      <tbody>${cats.map((c) => row(c))}${row({ name: "Total", ...T }, true)}</tbody></table></div></div>`;
}

// Newest first, of every kind; the buttons stay on top.
export function Reports({ d }) {
  const all = [...d.summaries.map((s) => ({ kind: "summary", r: s, at: s.created_at })),
    ...d.reports.map((r) => ({ kind: "report", r, at: r.created_at })),
    ...d.customs.filter((c) => c.answer != null).map((c) => ({ kind: "custom", r: c, at: c.answered_at }))]
    .sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
  return html`<div class="section-head"><h1>Reports</h1></div>
    <div class="sub">Written by a worker: an executive summary reports what changed since the one before; a resource use
      report sorts every done task into categories it draws up from scratch, with time, tokens and cost from the tasks' runs;
      a custom report answers your question in one paragraph.</div>
    <div style="margin:12px 0;display:grid;gap:8px">
      <${Request} d=${d} op="summary_request" open=${d.state.open_summary}
        label="Write an executive summary" queued="Executive summary queued" writing="Writing an executive summary…" />
      <${Request} d=${d} op="report_request" open=${d.state.open_report}
        label="Write a resource use report" queued="Resource use report queued" writing="Writing a resource use report…" />
      <${Ask} d=${d} /></div>
    ${!all.length ? html`<div class="empty">No report yet.</div>` : all.map(({ kind, r }) => html`<div style="margin-top:12px">
      ${kind === "summary" ? html`<div class="card">${head("executive summary", r)}<${Markdown} text=${r.body} /></div>`
        : kind === "custom" ? html`<div class="card">${head("custom report", { ...r, created_at: r.answered_at })}
          <blockquote class="question">${r.question}</blockquote><${Markdown} text=${r.answer} /></div>`
        : html`<${Report} r=${r} />`}</div>`)}`;
}
