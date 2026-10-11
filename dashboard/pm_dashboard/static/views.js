import { html, useState, useEffect, useRef } from "/vendor/preact-htm.js";
import { Markdown, Pill, Kind, Prio, TaskLink, Id, Time, Elapsed, Tile, Progress, Bars } from "/ui.js";
import { usd, tokens, stamp, duration, seconds, day } from "/data.js";
import { EditableBody, EditableLine, PrioEdit, post, autofocus } from "/editor.js";
import { AddButton, Welcome } from "/add.js";
import { TaskDiff } from "/diff.js";

const runningId = (d) => d.running && d.running.task_id;
const saveTask = (id, field) => (v) => post({ op: "task_edit", id, [field]: v });
const savePlan = (id, field) => (v) => post({ op: "plan_edit", id, [field]: v });
const saveMilestone = (id, field) => (v) => post({ op: "milestone_edit", id, [field]: v });

function TaskPrio({ d, t }) {
  const p = d.prio(t);
  return html`<${PrioEdit} value=${t.priority} effective=${p.value} from=${p.from === "plan" ? "plan" : "–"}
    save=${t.status === "open" ? saveTask(t.id, "priority") : null} disabled=${runningId(d) === t.id} />`;
}

const KINDS = ["work", "plan", "masterplan", "split", "cleanup"];

function Banners({ d }) {
  const s = d.state;
  return html`
    ${s.errors && s.errors.length ? html`<div class="banner error"><b>pm validate</b> finds problems in ${s.db}:
      <pre>${s.errors.join("\n")}</pre></div>` : null}
    ${s.head_signoff ? html`<div class="banner warn">The head of the queue, <a href=${`#/task/${d.queue[0]}`}>task ${d.queue[0]}</a>,
      needs a sign-off (${s.head_signoff}); <code>pm run</code> asks for it next.</div>` : null}`;
}

function RunningCard({ d }) {
  const r = d.running;
  if (!r) return html`<div class="card"><div class="sub">No worker is running${d.queue.length ? html`: start them with <code>pm run</code> in the project's terminal` : ""}.</div>
    ${d.queue.length ? html`<div class="muted" style="margin-top:4px">Next: <a href=${`#/task/${d.queue[0]}`}>task ${d.queue[0]}</a> ${d.task.get(d.queue[0]).subject}</div>` : null}</div>`;
  const t = d.task.get(r.task_id);
  return html`<div class="card">
    <div class="row"><${Pill} status="running" /><span class="sub">task ${t.id}, run ${r.id}, for <${Elapsed} start=${r.started_at} /></span></div>
    <div style="margin-top:6px;font-weight:600"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /></div>
    <div class="muted" style="margin-top:2px">${d.plan.get(t.plan_id).subject} · <a href=${`#/task/${t.id}?log`}>live log</a></div>
  </div>`;
}

export function Overview({ d }) {
  if (!d.milestones.length) return html`<${Welcome} />`;
  const open = d.milestones.filter((m) => m.status === "open");
  const pending = d.decisions.filter((x) => x.status === "pending");
  const finished = d.runs.filter((r) => r.result !== "running");
  const today = day(new Date().toISOString());
  const costToday = d.cost(finished.filter((r) => day(r.started_at) === today));
  return html`
    <h1>Overview</h1>
    <${Banners} d=${d} />
    <div class="grid2"><${RunningCard} d=${d} />
      <div class="card"><div class="section-head"><b>Waiting on you</b><a href="#/decisions">decisions</a></div>
        ${pending.length ? html`<ul style="margin:6px 0 0;padding-left:18px">${pending.slice(0, 6).map((x) => html`<li>
          <a href=${`#/decision/${x.id}`}>${x.subject}</a> ${x.signoff ? html`<span class="tag signoff">sign-off</span>` : null}</li>`)}</ul>
          ${pending.length > 6 ? html`<div class="muted">and ${pending.length - 6} more</div>` : null}`
          : html`<div class="sub" style="margin-top:6px">No pending decisions.</div>`}</div>
    </div>
    <div class="tiles">
      <${Tile} label="Queue" value=${d.queue.length} sub="workable tasks" />
      <${Tile} label="Open tasks" value=${d.tasks.filter((t) => t.status === "open").length} sub=${`of ${d.tasks.length}`} />
      <${Tile} label="Cost today" value=${usd(costToday)} sub=${`${usd(d.cost(finished))} in total`} />
      <${Tile} label="Runs" value=${finished.length} sub=${`${finished.filter((r) => r.result === "failed").length} failed`} />
    </div>
    <h2>Open milestones</h2>
    ${open.length ? html`<div class="cards">${open.map((m) => html`<${MilestoneCard} d=${d} m=${m} />`)}</div>`
      : html`<div class="empty">No open milestone. <code>pm milestone add</code> starts one.</div>`}
    <h2>Queue</h2>
    <${QueueTable} d=${d} ids=${d.queue.slice(0, 8)} />
    ${d.queue.length > 8 ? html`<div style="margin-top:6px"><a href="#/queue">All ${d.queue.length} tasks</a></div>` : null}`;
}

function counts(tasks) {
  return {
    done: tasks.filter((t) => t.status === "done").length,
    dropped: tasks.filter((t) => t.status === "dropped").length,
    open: tasks.filter((t) => t.status === "open").length,
  };
}

function MilestoneCard({ d, m }) {
  const tasks = d.milestoneTasks(m);
  const runs = tasks.flatMap(d.taskRuns);
  const plans = d.plansOf.get(m.id) || [];
  return html`<div class="card">
    <div class="row" style="justify-content:space-between"><a href=${`#/milestone/${m.id}`}><b>${m.subject}</b></a>
      <${Pill} status=${m.status} /></div>
    <${Progress} ...${counts(tasks)} />
    <div class="sub" style="margin-top:6px">${plans.length} plans · ${runs.length} runs · ${usd(d.cost(runs))} · created <${Time} ts=${m.created_at} /></div>
  </div>`;
}

function QueueTable({ d, ids }) {
  if (!ids.length) return html`<div class="empty">The queue is empty.</div>`;
  return html`<div class="tablewrap"><table>
    <thead><tr><th class="r">#</th><th>prio</th><th>task</th><th>subject</th><th>plan</th><th>milestone</th></tr></thead>
    <tbody>${ids.map((id) => {
      const t = d.task.get(id), p = d.plan.get(t.plan_id), m = d.milestone.get(p.milestone_id);
      const run = d.running && d.running.task_id === id;
      return html`<tr class=${d.queuePos.get(id) === 0 ? "head-row" : ""}>
        <td class="r num muted">${d.queuePos.get(id) + 1}</td><td><${Prio} p=${d.prio(t)} /></td>
        <td><${Id} kind="task" id=${id} /></td>
        <td class="subject"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /> ${run ? html`<${Pill} status="running" />` : null}</td>
        <td class="ink2"><a class="ink2" href=${`#/plan/${p.id}`}>${p.subject}</a></td><td class="muted">${m.subject}</td></tr>`;
    })}</tbody></table></div>`;
}

export function Queue({ d }) {
  const out = d.tasks.filter((t) => t.status === "open" && !d.queuePos.has(t.id));
  return html`<h1>Queue</h1>
    <div class="sub">What <code>pm run</code> works next: open tasks of open milestones that no pending decision blocks,
      highest priority first, then milestone, plan and task.</div>
    <${Banners} d=${d} />
    <h2>${d.queue.length} workable</h2>
    <${QueueTable} d=${d} ids=${d.queue} />
    <h2>Open, not in the queue (${out.length})</h2>
    ${out.length ? html`<div class="tablewrap"><table>
      <thead><tr><th>task</th><th>subject</th><th>why</th></tr></thead>
      <tbody>${out.map((t) => {
        const why = d.excluded(t);
        return html`<tr><td><${Id} kind="task" id=${t.id} /></td><td class="subject"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /></td>
          <td>${typeof why === "string" ? why : html`waits for ${why.map((x, i) => html`${i ? ", " : ""}<a href=${`#/decision/${x.id}`}>decision ${x.id}</a>`)}`}</td></tr>`;
      })}</tbody></table></div>` : html`<div class="empty">None.</div>`}`;
}

export function Milestones({ d }) {
  const ms = [...d.milestones].sort((a, b) => (a.status === b.status ? a.id - b.id : a.status === "open" ? -1 : 1));
  if (!ms.length) return html`<${Welcome} />`;
  return html`<h1>Milestones</h1>
    <div class="cards" style="margin-top:12px">${ms.map((m) => html`<${MilestoneCard} d=${d} m=${m} />`)}</div>
    <div style="margin-top:16px"><${AddButton} kind="milestone" /></div>`;
}

function TaskRows({ d, tasks }) {
  return html`<table><tbody>${tasks.map((t) => {
    const run = d.running && d.running.task_id === t.id;
    const pos = d.queuePos.get(t.id);
    const why = d.excluded(t);
    return html`<tr class=${t.status !== "open" ? "closed" : ""}>
      <td style="width:56px"><${Id} kind="task" id=${t.id} /></td>
      <td class="subject"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /></td>
      <td style="width:90px"><${TaskPrio} d=${d} t=${t} /></td>
      <td style="width:170px">${run ? html`<${Pill} status="running" />` : html`<${Pill} status=${t.status} />`}
        ${pos != null ? html` <span class="muted num" title="position in the queue">#${pos + 1}</span>` : null}
        ${why && typeof why !== "string" ? html` <span class="tag" title="waits for a pending decision">blocked</span>` : null}</td></tr>`;
  })}</tbody></table>`;
}

function Chips({ value, set, options }) {
  return html`<div class="chips">${options.map(([k, label]) => html`<button class=${"chip" + (value === k ? " on" : "")}
    onClick=${() => set(k)}>${label}</button>`)}</div>`;
}

export function Milestone({ d, id }) {
  const m = d.milestone.get(id);
  const [show, setShow] = useState("open");
  const [kind, setKind] = useState("all");
  if (!m) return html`<div class="empty">No milestone ${id}.</div>`;
  const tasks = d.milestoneTasks(m);
  const filt = (ts) => ts.filter((t) => (show === "all" || (show === "open" ? t.status === "open" : t.status !== "open"))
    && (kind === "all" || t.kind === kind));
  return html`<div class="crumbs"><a href="#/milestones">Milestones</a></div>
    <${EditableLine} text=${m.subject} save=${saveMilestone(m.id, "subject")} />
    <div class="row sub"><${Pill} status=${m.status} /><span>milestone ${m.id} · created ${stamp(m.created_at)}</span>
      ${m.implemented_at ? html`<span>· implemented ${stamp(m.implemented_at)}</span>` : null}</div>
    <${Progress} ...${counts(tasks)} />
    <div style="margin-top:12px"><${EditableBody} text=${m.body} save=${saveMilestone(m.id, "body")} /></div>
    <div class="section-head"><h2>Plans</h2></div>
    <${Chips} value=${show} set=${setShow} options=${[["open", "open tasks"], ["closed", "closed"], ["all", "all"]]} />
    <${Chips} value=${kind} set=${setKind} options=${[["all", "every kind"], ...KINDS.map((k) => [k, k])]} />
    ${d.plansSorted(m).map((p) => {
      const ts = filt(d.tasksOf.get(p.id) || []);
      const c = counts(d.tasksOf.get(p.id) || []);
      return html`<details class="plan" open=${ts.length > 0}>
        <summary><${PrioEdit} value=${p.priority} effective=${0} from="–" save=${savePlan(p.id, "priority")} /><a href=${`#/plan/${p.id}`} onClick=${(e) => e.stopPropagation()}><b>${p.subject}</b></a>
          <span class="muted">plan ${p.id} · ${c.open} open · ${c.done} done${c.dropped ? ` · ${c.dropped} dropped` : ""}</span></summary>
        <div class="tasks">${ts.length ? html`<${TaskRows} d=${d} tasks=${ts} />` : html`<div class="empty" style="padding:10px 12px">No tasks match.</div>`}</div>
      </details>`;
    })}
`;
}

export function Plan({ d, id }) {
  const p = d.plan.get(id);
  if (!p) return html`<div class="empty">No plan ${id}.</div>`;
  const m = d.milestone.get(p.milestone_id);
  const tasks = d.tasksOf.get(p.id) || [];
  return html`<div class="crumbs"><a href="#/milestones">Milestones</a> › <a href=${`#/milestone/${m.id}`}>${m.subject}</a></div>
    <${EditableLine} text=${p.subject} save=${savePlan(p.id, "subject")} />
    <div class="row sub"><span>plan ${p.id}</span><span>· priority <${PrioEdit} value=${p.priority} effective=${0} from="none"
      save=${savePlan(p.id, "priority")} /></span>
      <span>· created ${stamp(p.created_at)}</span>${p.created_by_task != null ? html`<span>· by <a href=${`#/task/${p.created_by_task}`}>task ${p.created_by_task}</a></span>` : null}</div>
    <${Progress} ...${counts(tasks)} />
    <div style="margin-top:12px"><${EditableBody} text=${p.body} save=${savePlan(p.id, "body")} /></div>
    <div class="sub" style="margin-top:4px">A new subject or body queues "create tasks for plan ${p.id}" again.</div>
    <h2>Tasks</h2>
    <div class="tablewrap">${tasks.length ? html`<${TaskRows} d=${d} tasks=${tasks} />` : html`<div class="empty" style="padding:10px 12px">No tasks yet.</div>`}</div>
    <div style="margin-top:12px"><${AddButton} kind="task" parent=${p.id} /></div>`;
}

function RunsTable({ d, runs, showTask = false }) {
  if (!runs.length) return html`<div class="empty">No runs.</div>`;
  return html`<div class="tablewrap"><table>
    <thead><tr><th>run</th>${showTask ? html`<th>task</th>` : null}<th>started</th><th class="r">duration</th><th>result</th>
      <th class="r">turns</th><th class="r">tokens</th><th class="r">cost</th><th>model</th><th>commit</th></tr></thead>
    <tbody>${[...runs].reverse().map((r) => html`<tr>
      <td class="num muted">${r.id}</td>
      ${showTask ? html`<td><a href=${`#/task/${r.task_id}`}>${d.task.get(r.task_id).subject}</a></td>` : null}
      <td class="num">${stamp(r.started_at)}</td>
      <td class="r"><${Elapsed} start=${r.started_at} end=${r.ended_at} /></td>
      <td><${Pill} status=${r.result} />${r.reason ? html`<div class="muted" style="font-size:12px;max-width:360px" title=${r.reason}>${r.reason.split("\n")[0].slice(0, 120)}</div>` : null}</td>
      <td class="r num">${r.turns ?? ""}</td><td class="r num">${tokens(d.tokens(r))}</td><td class="r num">${usd(r.cost_usd)}</td>
      <td class="muted">${r.model || ""}</td><td class="mono muted">${r.base_commit ? r.base_commit.slice(0, 7) : ""}</td></tr>`)}</tbody></table></div>`;
}

export function Log({ d, t, live, full = false }) {
  const [lines, setLines] = useState(null);
  const next = useRef(0);
  const box = useRef(null);
  const size = live && d.state.__log && d.state.__log.task === t.id ? d.state.__log.size : null;
  useEffect(() => { next.current = 0; setLines(null); }, [t.id]);
  useEffect(() => {
    let gone = false;
    fetch(`/api/log/${t.id}?from=${next.current}`).then((r) => r.json()).then((j) => {
      if (gone) return;
      const stick = box.current && box.current.scrollHeight - box.current.scrollTop - box.current.clientHeight < 40;
      if (j.next < next.current) return;
      next.current = j.next;
      setLines((old) => (old || []).concat(j.lines));
      if (stick || !box.current) requestAnimationFrame(() => box.current && (box.current.scrollTop = box.current.scrollHeight));
    });
    return () => { gone = true; };
  }, [t.id, size]);
  if (lines === null) return html`<div class="empty">Loading the log…</div>`;
  if (!lines.length) return html`<div class="empty">No log for this task.</div>`;
  return html`<div class=${"log" + (full ? " full" : "")} ref=${box}>${lines.map((l) => html`<div class=${l.startsWith("  → ") ? "tool" : /^\[\w+: \d+ turns/.test(l) ? "res" : ""}>${l || " "}</div>`)}</div>`;
}

export function Task({ d, id, query }) {
  const t = d.task.get(id);
  const [showLog, setShowLog] = useState(query === "log");
  useEffect(() => setShowLog(query === "log"), [id, query]);
  if (!t) return html`<div class="empty">No task ${id}.</div>`;
  const p = d.plan.get(t.plan_id), m = d.milestone.get(p.milestone_id);
  const runs = d.taskRuns(t);
  const running = d.running && d.running.task_id === t.id;
  const decs = d.decisionsOf.get(t.id) || [];
  const created = d.createdBy.get(t.id) || [];
  const pos = d.queuePos.get(t.id);
  const why = d.excluded(t);
  return html`<div class="crumbs"><a href="#/milestones">Milestones</a> › <a href=${`#/milestone/${m.id}`}>${m.subject}</a> › <a href=${`#/plan/${p.id}`}>${p.subject}</a></div>
    <${EditableLine} text=${t.subject} save=${saveTask(t.id, "subject")} disabled=${running} />
    <div class="row sub">${running ? html`<${Pill} status="running" />` : html`<${Pill} status=${t.status} />`}<${Kind} kind=${t.kind} />
      <span>task ${t.id}</span><span>· priority <${TaskPrio} d=${d} t=${t} /></span>
      ${pos != null ? html`<span>· #${pos + 1} in the queue</span>` : null}
      ${why && typeof why === "string" ? html`<span>· not queued: ${why}</span>` : null}</div>
    <dl class="meta">
      <dt>created</dt><dd>${stamp(t.created_at)}${t.created_by_task != null ? html` by <a href=${`#/task/${t.created_by_task}`}>task ${t.created_by_task}</a>` : " by the human or pm"}</dd>
      ${t.retired_at ? html`<dt>${t.status}</dt><dd>${stamp(t.retired_at)}</dd>` : null}
      ${t.note ? html`<dt>note</dt><dd>${t.note}</dd>` : null}
    </dl>
    ${running ? html`<div class="banner warn">A worker is on this task: it can't be changed until the run ends.</div>` : null}
    <${EditableBody} text=${t.body} save=${saveTask(t.id, "body")} disabled=${running} />
    ${t.status === "open" && !running ? html`<${CloseTask} t=${t} />` : null}
    ${decs.length ? html`<h2>Decisions</h2><ul>${decs.map((x) => html`<li><a href=${`#/decision/${x.id}`}>${x.subject}</a> <${Pill} status=${x.status} />
      ${x.signoff ? html` <span class="tag signoff">sign-off: ${x.signoff}</span>` : null}</li>`)}</ul>` : null}
    ${created.length ? html`<h2>Created by this task</h2><ul>${created.map(([k, r]) => html`<li>${k} <a href=${`#/${k}/${r.id}`}>${r.subject}</a></li>`)}</ul>` : null}
    <h2>Runs (${runs.length}${runs.length ? `, ${usd(d.cost(runs))}` : ""})</h2>
    <${RunsTable} d=${d} runs=${runs} />
    ${t.status !== "open" || runs.length ? html`<h2>Changes</h2><${TaskDiff} task=${t} version=${t.retired_at} />` : null}
    <div class="section-head"><h2>Worker log${running ? " (live)" : ""}</h2>
      <button class="iconbtn" onClick=${() => setShowLog(!showLog)}>${showLog ? "hide" : "show"}</button></div>
    ${showLog ? html`<${Log} d=${d} t=${t} live=${running} />` : null}`;
}

function CloseTask({ t }) {
  const [mode, setMode] = useState(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState(null);
  const today = new Date().toISOString().slice(0, 10);
  const go = async () => {
    try { await post({ op: "task_close", id: t.id, status: mode, note }); setMode(null); setError(null); }
    catch (e) { setError(e.message); }
  };
  if (!mode) return html`<div class="row" style="margin-top:10px">
    <button class="btn" onClick=${() => { setMode("done"); setNote(`${today}, `); }}>Done…</button>
    <button class="btn" onClick=${() => { setMode("dropped"); setNote(""); }}>Drop…</button></div>`;
  return html`<div class="row" style="margin-top:10px">
    <input class="note-input" ref=${autofocus} value=${note} placeholder=${mode === "done" ? "date, files" : "why it is dropped"}
      onInput=${(e) => setNote(e.target.value)} onKeyDown=${(e) => { if (e.key === "Enter") go(); if (e.key === "Escape") setMode(null); }} />
    <button class="btn primary" onClick=${go}>${mode === "done" ? "Mark done" : "Drop"}</button>
    <button class="iconbtn" onClick=${() => setMode(null)}>cancel</button>
    ${error ? html`<span class="err">${error}</span>` : null}</div>`;
}

// Pending decisions by milestone, the ones holding up the most important work first.
function impact(d, x) {
  const waiting = (d.tasksWaiting.get(x.id) || []).filter((t) => t.status === "open");
  const top = waiting.length ? Math.max(...waiting.map((t) => d.prio(t).value)) : null;
  const head = d.queue.length ? d.prio(d.task.get(d.queue[0])).value : null;
  const stalls = waiting.length > 0 && (head === null || top > head);
  return { waiting, top, stalls, milestone: waiting.length ? d.milestoneOfTask(waiting[0]) : null };
}

function PendingGroups({ d, xs }) {
  const items = xs.map((x) => ({ x, ...impact(d, x) }))
    .sort((a, b) => (b.stalls - a.stalls) || ((b.top ?? -1e9) - (a.top ?? -1e9)) || (b.waiting.length - a.waiting.length) || a.x.id - b.x.id);
  const groups = new Map();
  for (const it of items) {
    const k = it.milestone ? it.milestone.id : 0;
    (groups.get(k) || groups.set(k, []).get(k)).push(it);
  }
  return html`${[...groups].map(([k, its]) => html`<h3>${k ? html`<a href=${`#/milestone/${k}`}>${d.milestone.get(k).subject}</a>` : "No open task waits"}</h3>
    <div class="tablewrap"><table><tbody>${its.map(({ x, waiting, top, stalls }) => html`<tr>
      <td style="width:56px"><${Id} kind="decision" id=${x.id} /></td>
      <td class="subject"><a href=${`#/decision/${x.id}`}>${x.subject}</a> ${x.signoff ? html`<span class="tag signoff">${x.signoff}</span>` : null}
        ${x.options.length ? html`<div class="muted" style="font-size:12px">${x.options.join(" / ")}</div>` : null}</td>
      <td style="width:200px">${waiting.length ? html`holds up ${waiting.length} task${waiting.length > 1 ? "s" : ""}, prio ${top}` : html`<span class="muted">holds up nothing</span>`}
        ${stalls ? html`<div><span class="tag signoff" title="its tasks have a higher priority than the head of the queue">would run next</span></div>` : null}</td>
      <td class="muted" style="width:110px"><${Time} ts=${x.created_at} /></td></tr>`)}</tbody></table></div>`)}`;
}

export function Decisions({ d }) {
  const pending = d.decisions.filter((x) => x.status === "pending");
  const signoffs = pending.filter((x) => x.signoff), questions = pending.filter((x) => !x.signoff);
  const closed = d.decisions.filter((x) => x.status !== "pending").reverse();
  const list = (xs) => html`<div class="tablewrap"><table><tbody>${xs.map((x) => html`<tr>
    <td style="width:56px"><${Id} kind="decision" id=${x.id} /></td>
    <td class="subject"><a href=${`#/decision/${x.id}`}>${x.subject}</a> ${x.signoff ? html`<span class="tag signoff">${x.signoff}</span>` : null}
      ${x.status !== "pending" ? html`<div class="muted" style="font-size:12px">${x.answer || x.reason}</div>` : null}</td>
    <td style="width:110px"><${Pill} status=${x.status} /></td>
    <td class="muted" style="width:140px">${(d.tasksWaiting.get(x.id) || []).length} task(s)</td>
    <td class="muted" style="width:110px"><${Time} ts=${x.closed_at || x.created_at} /></td></tr>`)}</tbody></table></div>`;
  return html`<h1>Decisions</h1>
    <div class="sub">Answer them in a Claude session, with <code>pm decision answer</code>; <a href="#/since">since you last looked</a> shows what changed.</div>
    ${signoffs.length ? html`<h2>Sign-offs (${signoffs.length})</h2><${PendingGroups} d=${d} xs=${signoffs} />` : null}
    <h2>Questions (${questions.length})</h2>${questions.length ? html`<${PendingGroups} d=${d} xs=${questions} />` : html`<div class="empty">None pending.</div>`}
    <h2>Closed (${closed.length})</h2>${closed.length ? list(closed) : html`<div class="empty">None yet.</div>`}`;
}

const KNOWN = new Set(["id", "subject", "status", "options", "body", "answer", "reason", "created_at", "closed_at",
  "created_by_task", "signoff", "signoff_view"]);

export function Decision({ d, id }) {
  const x = d.decision.get(id);
  if (!x) return html`<div class="empty">No decision ${id}.</div>`;
  const waiting = d.tasksWaiting.get(x.id) || [];
  const extra = Object.entries(x).filter(([k, v]) => !KNOWN.has(k) && v != null);
  return html`<div class="crumbs"><a href="#/decisions">Decisions</a></div>
    <h1>${x.subject}</h1>
    <div class="row sub"><${Pill} status=${x.status} />${x.signoff ? html`<span class="tag signoff">sign-off: ${x.signoff}</span>` : null}
      <span>decision ${x.id} · created ${stamp(x.created_at)}</span>
      ${x.created_by_task != null ? html`<span>by <a href=${`#/task/${x.created_by_task}`}>task ${x.created_by_task}</a></span>` : null}
      ${extra.map(([k, v]) => html`<span class="tag">${k}: ${String(v)}</span>`)}</div>
    ${x.status !== "pending" ? html`<div class="card" style="margin:12px 0"><b>${x.status === "answered" ? "Answer" : "Irrelevant"}</b>
      (${stamp(x.closed_at)}): ${x.answer || x.reason}</div>` : null}
    ${x.options.length ? html`<h3>Options</h3><ol class="options">${x.options.map((o, i) => html`<li class=${i === 0 ? "rec" : ""}>${o}</li>`)}</ol>` : null}
    ${x.body.trim() || !x.signoff_view ? html`<div class="card" style="margin-top:12px"><${Markdown} text=${x.body} empty="No body." /></div>` : null}
    ${x.signoff_view ? html`<h2>What it approves</h2><div class="card"><${Markdown} text=${x.signoff_view} /></div>` : null}
    <h2>Tasks waiting for it (${waiting.length})</h2>
    ${waiting.length ? html`<div class="tablewrap"><${TaskRows} d=${d} tasks=${waiting} /></div>` : html`<div class="empty">None.</div>`}`;
}

export function Runs({ d }) {
  const [res, setRes] = useState("all");
  const finished = d.runs.filter((r) => r.result !== "running");
  const byDay = new Map();
  for (const r of finished) byDay.set(day(r.started_at), (byDay.get(day(r.started_at)) || []).concat(r));
  const days = [...byDay.keys()].sort();
  const series = [];
  if (days.length) {
    for (let t = new Date(days[0] + "T12:00:00"); day(t.toISOString()) <= days[days.length - 1]; t.setDate(t.getDate() + 1)) {
      const k = day(t.toISOString()), rs = byDay.get(k) || [];
      series.push({ label: k, short: k.slice(5), value: d.cost(rs), extra: `${rs.length} runs` });
    }
  }
  const kindOf = (r) => d.task.get(r.task_id).kind;
  const byKind = KINDS.map((k) => ({ label: k, value: d.cost(finished.filter((r) => kindOf(r) === k)) })).filter((x) => x.value > 0);
  const byMilestone = d.milestones.map((m) => ({ label: m.subject.length > 16 ? m.subject.slice(0, 15) + "…" : m.subject,
    value: d.cost(d.milestoneTasks(m).flatMap(d.taskRuns)) })).filter((x) => x.value > 0);
  const tok = finished.reduce((s, r) => s + d.tokens(r), 0);
  const cacheRead = finished.reduce((s, r) => s + (r.cache_read_tokens || 0), 0);
  const secs = finished.reduce((s, r) => s + seconds(r.started_at, r.ended_at), 0);
  const shown = res === "all" ? d.runs : d.runs.filter((r) => r.result === res);
  return html`<h1>Runs and cost</h1>
    <div class="tiles">
      <${Tile} label="Cost" value=${usd(d.cost(finished))} sub=${`${finished.length} runs`} />
      <${Tile} label="Worker time" value=${duration(secs)} />
      <${Tile} label="Tokens" value=${tokens(tok) || "0"} sub=${tok ? `${Math.round((100 * cacheRead) / tok)}% cache reads` : ""} />
      <${Tile} label="Failed" value=${finished.filter((r) => r.result === "failed").length}
        sub=${`${finished.filter((r) => r.result === "limit").length} usage limits`} />
    </div>
    <div class="card"><b>Cost per day</b><${Bars} data=${series} fmt=${usd} /></div>
    <div class="grid2" style="margin-top:12px">
      <div class="card"><b>Cost per task kind</b><${Bars} data=${byKind} fmt=${usd} horizontal /></div>
      <div class="card"><b>Cost per milestone</b><${Bars} data=${byMilestone} fmt=${usd} horizontal /></div>
    </div>
    <h2>All runs</h2>
    <${Chips} value=${res} set=${setRes} options=${[["all", "all"], ...["running", "done", "blocked", "failed", "limit"].map((k) => [k, k])]} />
    <${RunsTable} d=${d} runs=${shown} showTask />`;
}
