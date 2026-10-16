import { html, useState, useEffect, useRef, useLayoutEffect } from "/vendor/preact-htm.js";
import { Markdown, InlineMd, Pill, Kind, Worktree, Prio, TaskLink, Id, Ref, Named, NamedLink, Titled, Time, Elapsed, Tile, Progress, Bars, Fold } from "/ui.js";
import { usd, tokens, stamp, duration, seconds, day } from "/data.js";
import { EditableBody, EditableLine, PrioEdit, post, autofocus } from "/editor.js";
import { AddButton, Welcome } from "/add.js";
import { TaskDiff, useCommit } from "/diff.js";
import { Affected } from "/affected.js";
import { Resolve } from "/resolve.js";

// A text edit sends the text it started from as `base`: the server refuses it if the row changed meanwhile.
const saver = (op) => (id, field) => (v, base) => post({ op, id, [field]: v, ...(base != null ? { base: { [field]: base } } : {}) });
const saveTask = saver("task_edit");
const savePlan = saver("plan_edit");
const planSave = (d, id, field) => (d.planDone.has(id) ? null : savePlan(id, field));
const saveMilestone = saver("milestone_edit");

function TaskPrio({ d, t }) {
  const p = d.prio(t);
  return html`<${PrioEdit} value=${t.priority} effective=${p.value} from=${p.from === "plan" ? "plan" : "–"}
    save=${t.status === "open" ? saveTask(t.id, "priority") : null} disabled=${d.isRunning(t.id)} />`;
}

// The plain-English intro on top; the technical body right below it, hidden behind a button unless there's no intro.
function Intro({ intro, saveIntro, children }) {
  const [open, setOpen] = useState(!intro);
  return html`${intro || saveIntro ? html`<div class="intro">${saveIntro ? html`<${EditableBody} text=${intro} save=${saveIntro} empty="No intro." ph="Intro: plain English for an outsider" />`
      : html`<div class="card"><${Markdown} text=${intro} /></div>`}</div>` : null}
    ${intro && children ? html`<button class="iconbtn details-btn" onClick=${() => setOpen(!open)}>${open ? "Hide details" : "Show details"}</button>` : null}
    ${open ? html`<div class="details-body">${children}</div>` : null}`;
}

const KINDS = ["work", "plan", "masterplan", "split", "cleanup", "revise", "investigate", "merge", "exec"];

function Banners({ d }) {
  const s = d.state;
  return html`
    ${s.errors && s.errors.length ? html`<div class="banner error"><b>pm validate</b> finds problems in ${s.db}:
      <pre>${s.errors.join("\n")}</pre></div>` : null}
    ${s.head_signoff ? html`<div class="banner warn">The head of the queue, <a href=${`#/task/${d.queue[0]}`}>task ${d.queue[0]}</a>,
      needs a sign-off (${s.head_signoff}); <code>pm run</code> asks for it next.</div>` : null}`;
}

// The running run resumed an earlier one's Claude session: which, and how far along the chain (warm_handoff).
export function Handoff({ d, r }) {
  const prev = r.continues_run != null && d.run.get(r.continues_run);
  if (!prev) return null;
  const c = d.chain(r), max = d.state.warm_handoff;
  return html`<div class="muted handoff">↳ same Claude session as <${TaskLink} t=${d.task.get(prev.task_id)} /> (run ${prev.id}),
    task ${c.indexOf(r) + 1}${max ? ` of at most ${max}` : ""}</div>`;
}

function RunningCard({ d }) {
  const r = d.running;
  if (!r && (d.stale || d.crash)) return html`<div class="card"><div class="row"><${Pill} status="failed" label="pm not running" />
      <span class="sub"><code>pm run</code> crashed${d.stale ? html` during <${TaskLink} t=${d.task.get(d.stale.task_id)} />` : ""}.</span></div>
    ${d.crash ? html`<pre class="crash">${d.crash.error}</pre>` : null}</div>`;
  if (!r) return html`<div class="card"><div class="sub">No worker is running${d.queue.length ? html`: start them with <code>pm run</code> in the project's terminal` : ""}.</div>
    ${d.queue.length ? html`<div class="muted" style="margin-top:4px">Next: <${TaskLink} t=${d.task.get(d.queue[0])} /></div>` : null}</div>`;
  return d.runningAll.map((r) => {
    const t = d.task.get(r.task_id);
    return html`<div class="card">
    <div class="row"><${Pill} status="running" /><span class="sub">task ${t.id}, run ${r.id}, for <${Elapsed} start=${r.started_at} /></span><${Worktree} name=${r.worktree} /></div>
    <div style="margin-top:6px;font-weight:600"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /></div>
    <div class="muted" style="margin-top:2px">${t.plan_id != null ? html`<${Named} kind="plan" r=${d.plan.get(t.plan_id)} /> · ` : null}<a href=${`#/task/${t.id}?log`}>live log</a></div>
    <${Handoff} d=${d} r=${r} />
  </div>`;
  });
}

export function Overview({ d }) {
  if (!d.milestones.length) return html`<${Welcome} />`;
  const open = d.milestones.filter((m) => m.status !== "implemented").sort((a, b) => a.queue_index - b.queue_index || a.id - b.id);
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

export function counts(tasks) {
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
  return html`<div class=${"card" + (d.held(m) ? " held" : "")}>
    <div class="row" style="justify-content:space-between"><a href=${`#/milestone/${m.id}`}><b><${Named} kind="milestone" r=${m} /></b></a>
      <${Pill} status=${m.status} /></div>
    <${Progress} ...${counts(tasks)} />
    <div class="sub" style="margin-top:6px"><span title="queue index: tasks of lower ones are worked first">queue ${m.queue_index}</span> · ${plans.length} plans · ${runs.length} runs · ${usd(d.cost(runs))} · created <${Time} ts=${m.created_at} /></div>
    ${m.status !== "implemented" ? (() => {
      const n = d.milestoneDecisions(m).length;
      return html`<div class="sub" style="margin-top:4px">${n ? html`<a class="tag signoff" href=${`#/decisions?milestone=${m.id}`}
        title="pending decisions its open tasks wait for">${n} pending decision${n > 1 ? "s" : ""}</a>` : "no pending decisions"}</div>`;
    })() : null}
  </div>`;
}

function QueueTable({ d, ids }) {
  if (!ids.length) return html`<div class="empty">The queue is empty.</div>`;
  return html`<div class="tablewrap"><table>
    <thead><tr><th class="r">#</th><th>prio</th><th>task</th><th>plan</th><th>milestone</th></tr></thead>
    <tbody>${ids.map((id) => {
      const t = d.task.get(id), p = d.plan.get(t.plan_id), m = d.milestoneOfTask(t);
      const run = d.isRunning(id);
      return html`<tr class=${d.queuePos.get(id) === 0 ? "head-row" : ""}>
        <td class="r num muted">${d.queuePos.get(id) + 1}</td><td><${Prio} p=${d.prio(t)} /></td>
        <td class="subject"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /> <${Worktree} name=${t.worktree} /> ${run ? html`<${Pill} status="running" />` : null}</td>
        <td class="ink2">${p ? html`<a class="ink2" href=${`#/plan/${p.id}`}><${Named} kind="plan" r=${p} /></a>` : html`<a class="ink2" href="#/tasks">executive intervention</a>`}</td><td class="muted">${m ? html`<${Named} kind="milestone" r=${m} />` : null}</td></tr>`;
    })}</tbody></table></div>`;
}

export function Queue({ d }) {
  const out = d.tasks.filter((t) => t.status === "open" && !d.queuePos.has(t.id));
  return html`<h1>Queue</h1>
    <div class="sub">What <code>pm run</code> works next: open tasks of open milestones that no pending decision blocks (and exec tasks, as if at queue index 0),
      by the milestone's queue index (lowest first), then highest priority first, then milestone, plan and task.</div>
    <${Banners} d=${d} />
    <h2>${d.queue.length} workable</h2>
    <${QueueTable} d=${d} ids=${d.queue} />
    <h2>Open, not in the queue (${out.length})</h2>
    ${out.length ? html`<div class="tablewrap"><table>
      <thead><tr><th>task</th><th>why</th></tr></thead>
      <tbody>${out.map((t) => {
        const why = d.excluded(t);
        return html`<tr><td class="subject"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /></td>
          <td>${typeof why === "string" ? why : html`waits for ${why.map((x, i) => html`${i ? ", " : ""}<a href=${`#/decision/${x.id}`}>decision ${x.id}</a>`)}`}</td></tr>`;
      })}</tbody></table></div>` : html`<div class="empty">None.</div>`}`;
}

export function Milestones({ d }) {
  const order = { open: 0, "on-hold": 1 };
  if (!d.milestones.length) return html`<${Welcome} />`;
  const ms = d.milestones.filter((m) => m.status !== "implemented")
    .sort((a, b) => a.queue_index - b.queue_index || order[a.status] - order[b.status] || a.id - b.id);
  const done = d.milestones.filter((m) => m.status === "implemented")
    .sort((a, b) => (b.implemented_at || "").localeCompare(a.implemented_at || "") || b.id - a.id);
  const groups = new Map();
  for (const m of ms) groups.set(m.queue_index, [...(groups.get(m.queue_index) || []), m]);
  return html`<h1>Milestones</h1>
    <div class="sub">Grouped by queue index: tasks of a lower one are worked first.</div>
    <div style="margin-top:16px"><${AddButton} kind="milestone" /></div>
    <div class="ms-cols">
      <div>${[...groups].map(([q, xs]) => html`<h2>Queue index ${q}</h2>
          <div class="cards">${xs.map((m) => html`<${MilestoneCard} d=${d} m=${m} />`)}</div>`)}
        ${ms.length ? null : html`<div class="empty" style="margin-top:16px">No open milestone.</div>`}</div>
      <div class="ms-done"><h2>Completed (${done.length})</h2>
        ${done.length ? html`<div class="cards one">${done.map((m) => html`<${MilestoneCard} d=${d} m=${m} />`)}</div>`
          : html`<div class="empty">None yet.</div>`}</div>
    </div>`;
}

export function TaskRows({ d, tasks }) {
  return html`<table><tbody>${tasks.map((t) => {
    const run = d.isRunning(t.id);
    const pos = d.queuePos.get(t.id);
    const why = d.excluded(t);
    return html`<tr class=${t.status !== "open" ? "closed" : ""}>
      <td class="subject"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /> <${Worktree} name=${t.worktree} /></td>
      <td style="width:90px"><${TaskPrio} d=${d} t=${t} /></td>
      <td style="width:170px">${run ? html`<${Pill} status="running" />` : html`<${Pill} status=${d.taskState(t)} />`}
        ${pos != null ? html` <span class="muted num" title="position in the queue">#${pos + 1}</span>` : null}
        ${why && typeof why !== "string" ? html` <span class="tag" title="waits for a pending decision">blocked</span>` : null}</td></tr>`;
  })}</tbody></table>`;
}

export function Chips({ value, set, options }) {
  return html`<div class="chips">${options.map(([k, label]) => html`<button class=${"chip" + (value === k ? " on" : "")}
    onClick=${() => set(k)}>${label}</button>`)}</div>`;
}

export function Milestone({ d, id }) {
  const m = d.milestone.get(id);
  const [show, setShow] = useState("open");
  const [kind, setKind] = useState("all");
  if (!m) return html`<div class="empty">No milestone ${id}.</div>`;
  const tasks = d.milestoneTasks(m);
  const master = tasks.find((t) => t.kind === "masterplan");
  const mDone = m.status === "implemented" || (master && d.planDone.has(master.plan_id));
  const filt = (ts) => ts.filter((t) => (show === "all" || (show === "open" ? t.status === "open" : t.status !== "open"))
    && (kind === "all" || t.kind === kind));
  return html`<div class="crumbs"><a href="#/milestones">Milestones</a></div>
    <${Titled} kind="milestone" id=${m.id}><${EditableLine} text=${m.subject} save=${mDone ? null : saveMilestone(m.id, "subject")} /></${Titled}>
    <div class="row sub"><${Pill} status=${m.status} /><span title="tasks of lower queue indexes are worked first">queue index
      <${PrioEdit} what="queue index" value=${m.queue_index} effective=${0} from="" save=${m.status === "implemented" ? null : saveMilestone(m.id, "queue_index")} /></span>
      <span>created ${stamp(m.created_at)}</span>
      ${m.implemented_at ? html`<span>· implemented ${stamp(m.implemented_at)}</span>` : null}
      ${m.status !== "implemented" ? html`<${HoldButton} m=${m} />` : null}</div>
    ${d.held(m) ? html`<div class="hold-note">On hold: its plans and tasks stay out of the queue until it is resumed.</div>` : null}
    <${Progress} ...${counts(tasks)} />
    <div style="margin-top:12px"><${EditableBody} text=${m.body} save=${mDone ? null : saveMilestone(m.id, "body")} /></div>
    <div class="sub" style="margin-top:4px">${mDone ? "Done: it can't be changed; add a new milestone instead."
      : 'A new subject or body queues "revise the masterplan".'}</div>
    <div class="section-head"><h2>Plans</h2></div>
    <${Chips} value=${show} set=${setShow} options=${[["open", "open tasks"], ["closed", "closed"], ["all", "all"]]} />
    <${Chips} value=${kind} set=${setKind} options=${[["all", "every kind"], ...KINDS.map((k) => [k, k])]} />
    ${d.plansSorted(m).map((p) => {
      const ts = filt(d.tasksOf.get(p.id) || []);
      const c = counts(d.tasksOf.get(p.id) || []);
      return html`<details class="plan" open=${ts.length > 0}>
        <summary><${PrioEdit} value=${p.priority} effective=${0} from="–" save=${planSave(d, p.id, "priority")} /><a href=${`#/plan/${p.id}`} onClick=${(e) => e.stopPropagation()}><b><${Named} kind="plan" r=${p} /></b></a>
          ${d.held(m) && c.open ? html`<${Pill} status="on-hold" />` : null}<span class="muted">${c.open} open · ${c.done} done${c.dropped ? ` · ${c.dropped} dropped` : ""}</span></summary>
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
  return html`<div class="crumbs"><a href="#/milestones">Milestones</a> › <${NamedLink} kind="milestone" r=${m} /></div>
    <${Titled} kind="plan" id=${p.id}><${EditableLine} text=${p.subject} save=${planSave(d, p.id, "subject")} /></${Titled}>
    <div class="row sub">${d.held(m) && !d.planDone.has(p.id) ? html`<${Pill} status="on-hold" />` : null}<span>priority <${PrioEdit} value=${p.priority} effective=${0} from="none"
      save=${planSave(d, p.id, "priority")} /></span>
      <span>· created ${stamp(p.created_at)}</span>${p.created_by_task != null ? html`<span>· by <a href=${`#/task/${p.created_by_task}`}>task ${p.created_by_task}</a></span>` : null}</div>
    ${d.held(m) && !d.planDone.has(p.id) ? html`<div class="hold-note">On hold with <a href=${`#/milestone/${m.id}`}>milestone ${m.id}</a>: its tasks stay out of the queue.</div>` : null}
    <${Intro} key=${p.id} intro=${p.intro} saveIntro=${planSave(d, p.id, "intro")}>
      <${EditableBody} text=${p.body} save=${planSave(d, p.id, "body")} />
      <div class="sub" style="margin-top:4px">${d.planDone.has(p.id)
        ? "Done: no open task, nor in the plans below it. It can't be changed; add a new plan instead."
        : html`A new subject or body queues "create tasks for plan ${p.id}: ${p.subject}" again and the revision of the plans below it.`}</div></${Intro}>
    <${Progress} ...${counts(tasks)} />
    <h2>Tasks</h2>
    <div class="tablewrap">${tasks.length ? html`<${TaskRows} d=${d} tasks=${tasks} />` : html`<div class="empty" style="padding:10px 12px">No tasks yet.</div>`}</div>
    ${d.planDone.has(p.id) ? null : html`<div style="margin-top:12px"><${AddButton} kind="task" parent=${p.id} worktrees=${d.state.worktrees} /></div>`}`;
}

function RunsTable({ d, runs, showTask = false }) {
  if (!runs.length) return html`<div class="empty">No runs.</div>`;
  return html`<div class="tablewrap"><table>
    <thead><tr><th>run</th>${showTask ? html`<th>task</th>` : null}<th>started</th><th class="r">duration</th><th>result</th>
      <th class="r">turns</th><th class="r">tokens</th><th class="r">cost</th><th title="warm hand-off: runs that worked one Claude session, one after the other">session</th><th>model</th><th>commit</th></tr></thead>
    <tbody>${[...runs].reverse().map((r) => html`<tr>
      <td class="num muted">${r.id}</td>
      ${showTask ? html`<td><${TaskLink} t=${d.task.get(r.task_id)} /></td>` : null}
      <td class="num">${stamp(r.started_at)}</td>
      <td class="r"><${Elapsed} start=${r.started_at} end=${r.ended_at || (d.stale === r && d.crash ? d.crash.at : null)} /></td>
      <td>${d.stale === r ? html`<${Pill} status="failed" label="crashed" />` : html`<${Pill} status=${r.result} />`}${r.reason ? html`<div class="muted reason" style="font-size:12px;max-width:520px" title=${r.reason}>${reasonText(r)}</div>` : null}</td>
      <td class="r num">${r.turns ?? ""}</td><td class="r num">${tokens(d.tokens(r))}</td><td class="r num">${usd(r.cost_usd)}</td>
      <td class="num muted">${(() => {
        const c = d.chain(r);
        if (c.length < 2) return "";
        const i = c.indexOf(r), prev = c[i - 1];
        return html`<span class="tag" title=${prev ? `resumed the session of run ${prev.id} (task ${prev.task_id})` : `its session went on in run ${c[1].id} (task ${c[1].task_id})`}>${i ? "↳ " : ""}${i + 1}/${c.length}</span>`;
      })()}</td>
      <td class="muted">${r.model || ""}${r.worktree ? html` <${Worktree} name=${r.worktree} />` : null}</td><td class="mono muted">${r.base_commit ? r.base_commit.slice(0, 7) : ""}</td></tr>`)}</tbody></table></div>`;
}

// A failed run's reason is pm's summary: show it whole; others: the first line.
export const reasonText = (r) => r.result === "failed" ? r.reason : r.reason.split("\n")[0].slice(0, 200);

const MAX_LINES = 5000;

// One fetch at a time: a size change while one is out fetches again when it returns, from where it ended.
// A box 20 lines high that opens at its end and follows it while scrolled to the end. reveal: scroll it into view once.
const clock = (ts) => { const d = new Date(ts); return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":"); };

export function Log({ d, t, live, run = null, reveal = false }) {
  const [lines, setLines] = useState(null);
  const [cut, setCut] = useState(0);
  const f = useRef({ gen: 0, next: 0, busy: false, again: false });
  const box = useRef(null), stick = useRef(true);
  useLayoutEffect(() => {
    if (box.current && stick.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);
  useEffect(() => { if (reveal && lines && box.current) box.current.scrollIntoView({ block: "nearest" }); }, [reveal, lines === null]);
  const size = (live && (d.state.__log || []).find((l) => l.task === t.id) || {}).size ?? null;
  useEffect(() => { f.current = { gen: f.current.gen + 1, next: 0, busy: false, again: false }; stick.current = true; setLines(null); setCut(0); }, [t.id, run]);
  useEffect(() => {
    const s = f.current;
    if (s.busy) { s.again = true; return; }
    const load = () => {
      const gen = s.gen;
      s.busy = true;
      fetch(`/api/log/${t.id}?from=${s.next}${run != null ? `&run=${run}` : ""}`).then((r) => r.json()).then((j) => {
        if (f.current.gen !== gen) return;
        const b = box.current;
        stick.current = !b || b.scrollHeight - b.scrollTop - b.clientHeight < 40;
        s.next = j.next;
        setLines((old) => {
          const all = (old || []).concat(j.lines);
          if (all.length <= MAX_LINES) return all;
          setCut((c) => c + all.length - MAX_LINES);
          return all.slice(-MAX_LINES);
        });
      }).catch(() => {}).finally(() => {
        if (f.current.gen !== gen) return;
        s.busy = false;
        if (s.again) { s.again = false; load(); }
      });
    };
    load();
  }, [t.id, size, run]);
  if (lines === null) return html`<div class="empty">Loading the log…</div>`;
  if (!lines.length) return html`<div class="empty">No log for this task.</div>`;
  return html`<div class="log" ref=${box}>${cut ? html`<div class="muted">… ${cut} earlier lines</div>` : null}
    ${lines.map(([ts, l]) => html`<div class=${"line " + (l.startsWith("  → ") ? "tool" : /^\[\w+: \d+ turns/.test(l) ? "res" : "")}>
      <span class="ts">${ts ? clock(ts) : ""}</span><span>${l || " "}</span></div>`)}</div>`;
}

// The commit pm made for a closed task: the rows it touched in tasks.sql, its files, the full diff (hidden until shown).
function Commit({ d, t }) {
  const c = useCommit(t, t.retired_at);
  if (!c) return html`<h2>Changes</h2><div class="empty">Loading the commit…</div>`;
  if (!c.commit) return html`<h2>Affected</h2><div class="muted">Nothing: no commit by <code>pm run</code> for this task.</div>`;
  return html`<${Affected} d=${d} t=${t} rows=${c.rows} />
    <h2>Changes <span class="muted mono">${c.commit.slice(0, 7)}</span></h2>
    <table class="compact"><tbody>${c.files.map((f) => html`<tr><td class="mono">${f.file}</td>
      <td class="num r plus">${f.added != null ? `+${f.added}` : ""}</td><td class="num r minus">${f.deleted != null ? `−${f.deleted}` : "bin"}</td></tr>`)}</tbody></table>
    <${Fold} key=${"diff" + t.id} head=${html`<h2>Full diff</h2>`}><${TaskDiff} task=${t} c=${c} /></${Fold}>`;
}

// The worker's model for an open task: the task's own, or pm's pick ([models]: by kind, escalated after a failure).
function ModelPick({ d, t, disabled }) {
  const [error, setError] = useState(null);
  const [pick, why] = t.model_pick || [t.model, "set"];
  const auto = why === "set" ? null : `${pick || "claude default"} (${why === "kind" ? `by kind` : "escalated after a failure"})`;
  if (t.status !== "open" || disabled) return html`<span>${t.model || auto}</span>`;
  const kindPick = (d.state.models || {})[t.kind] ?? (d.state.models || {}).default;
  const names = [...new Set(["sonnet", "opus", ...(t.model ? [t.model] : [])])];
  const change = async (v) => {
    try { await post({ op: "task_edit", id: t.id, model: v || null }); setError(null); } catch (e) { setError(e.message); }
  };
  return html`<select class="small" value=${t.model || ""} onChange=${(e) => change(e.target.value)}
      title="the model of this task's next run; pm's pick: by kind ([models]), Opus after a failed run">
      <option value="">${auto || `pm's pick (${kindPick || "claude default"})`}</option>
      ${names.map((n) => html`<option value=${n}>${n}</option>`)}</select>
    ${error ? html` <span class="err">${error}</span>` : null}`;
}

// Exec, planning and cleanup tasks run in the trunk (pm/__init__.py: TRUNK_KINDS).
const TRUNK_KINDS = ["exec", "masterplan", "split", "revise", "cleanup"];

// A task's work tree, one of [worktrees] in the config; a select while the task is open and not running.
function WorktreePick({ d, t, disabled }) {
  const [error, setError] = useState(null);
  const names = d.state.worktrees || {};
  if (TRUNK_KINDS.includes(t.kind) || !t.worktree && t.kind === "investigate") return html`<span>trunk</span>`;
  if (t.status !== "open" || disabled) return html`<span title=${names[t.worktree] || ""}>${t.worktree || "none"}</span>`;
  const change = async (v) => {
    try { await post({ op: "task_edit", id: t.id, worktree: v }); setError(null); } catch (e) { setError(e.message); }
  };
  return html`<select class="small" value=${t.worktree || ""} onChange=${(e) => change(e.target.value)}
      title="tasks of different work trees run in parallel; a work tree is merged into the trunk after its task">
      ${t.worktree && names[t.worktree] ? null : html`<option value="">${t.worktree ? `${t.worktree} (not in [worktrees])` : "pick one"}</option>`}
      ${Object.entries(names).map(([n, why]) => html`<option value=${n}>${n}: ${why}</option>`)}</select>
    ${error ? html` <span class="err">${error}</span>` : null}`;
}

// The task page; the live page shows the same for its selected task (`embedded`: with a link to this page).
export function Task({ d, id, query, embedded = false }) {
  const t = d.task.get(id);
  if (!t) return html`<div class="empty">No task ${id}.</div>`;
  const p = d.plan.get(t.plan_id), m = d.milestoneOfTask(t);
  const runs = d.taskRuns(t);
  const running = d.isRunning(t.id);
  const decs = d.decisionsOf.get(t.id) || [];
  const created = d.createdBy.get(t.id) || [];
  const pos = d.queuePos.get(t.id);
  const why = d.excluded(t);
  const last = runs.filter((r) => r.ended_at).pop();
  const fail = t.status === "open" && last && last.result === "failed" ? last : null;
  return html`<div class="crumbs row"><span class="grow">${p ? html`<a href="#/milestones">Milestones</a> › <${NamedLink} kind="milestone" r=${m} /> › <${NamedLink} kind="plan" r=${p} />` : html`<a href="#/tasks">Tasks</a> › executive intervention`}</span>
      ${embedded ? html`<a class="btn" href=${`#/task/${t.id}`}>Task page →</a>` : null}</div>
    <${Titled} kind="task" id=${t.id}><${EditableLine} text=${t.subject} save=${t.status === "open" ? saveTask(t.id, "subject") : null} disabled=${running} /></${Titled}>
    <div class="row sub">${running ? html`<${Pill} status="running" />` : html`<${Pill} status=${d.taskState(t)} />`}<${Kind} kind=${t.kind} />
      <span>priority <${TaskPrio} d=${d} t=${t} /></span>
      ${running ? html`<span>· <${Elapsed} start=${d.runningOf.get(t.id).started_at} /></span>` : null}
      ${pos != null && !running ? html`<span>· ${pos === 0 ? "next in the queue" : `#${pos + 1} in the queue`}</span>` : null}
      ${why && typeof why === "string" ? html`<span>· not queued: ${why}</span>` : null}</div>
    <dl class="meta">
      <dt>created</dt><dd>${stamp(t.created_at)}${t.created_by_task != null ? html` by <a href=${`#/task/${t.created_by_task}`}>task ${t.created_by_task}</a>` : " by the human or pm"}</dd>
      ${t.retired_at ? html`<dt>${t.status}</dt><dd>${stamp(t.retired_at)} · <${Time} ts=${t.retired_at} /></dd>` : null}
      ${t.note ? html`<dt>note</dt><dd>${t.note}</dd>` : null}
      ${t.kind !== "exec" ? html`<dt>work tree</dt><dd><${WorktreePick} d=${d} t=${t} disabled=${running} /></dd>` : null}
      ${t.model_pick || t.model ? html`<dt>model</dt><dd><${ModelPick} d=${d} t=${t} disabled=${running} /></dd>` : null}
      ${runs.length ? html`<dt>runs</dt><dd>${runs.length}, ${duration(runs.reduce((s, r) => s + seconds(r.started_at, r.ended_at), 0))}, ${usd(d.cost(runs))}</dd>` : null}
    </dl>
    ${running ? html`<div class="banner warn">A worker is on this task: it can't be changed until the run ends.</div>` : null}
    ${fail ? html`<div class="banner error"><b>Run ${fail.id} failed</b> <span class="muted">${stamp(fail.ended_at)}</span><pre>${fail.reason || ""}</pre></div>` : null}
    ${why && typeof why !== "string" ? html`<div class="banner warn">Waits for ${why.map((x, i) => html`${i ? ", " : ""}<a href=${`#/decision/${x.id}`}>decision ${x.id}: ${x.subject}</a>`)}.</div>` : null}
    ${t.status === "open" && !running ? html`<${CloseTask} t=${t} />` : null}
    ${decs.length ? html`<h2>Decisions</h2><ul>${decs.map((x) => html`<li><a href=${`#/decision/${x.id}`}>${x.subject}</a> <${Pill} status=${x.status} />
      ${x.signoff ? html` <span class="tag signoff">sign-off: ${x.signoff}</span>` : null}</li>`)}</ul>` : null}
    ${created.length && t.status === "open" ? html`<h2>Created by this task</h2><ul>${created.map(([k, r]) => html`<li>${k} ${k === "decision" ? html`<a href=${`#/${k}/${r.id}`}>${r.subject}</a>` : html`<${NamedLink} kind=${k} r=${r} />`}</li>`)}</ul>` : null}
    ${t.status !== "open" ? html`<${Commit} d=${d} t=${t} />` : null}
    <h2>Runs (${runs.length}${runs.length ? `, ${usd(d.cost(runs))}` : ""})</h2>
    <${RunsTable} d=${d} runs=${runs} />
    ${runs.length ? html`<h2>Worker log${running ? " (live)" : fail ? ` (failed run ${fail.id})` : ""}</h2><${Log} d=${d} t=${t} live=${running} reveal=${query === "log"} />` : null}
    <h2>Body</h2>
    <${EditableBody} text=${t.body} save=${t.status === "open" ? saveTask(t.id, "body") : null} disabled=${running} />`;
}

function HoldButton({ m }) {
  const [error, setError] = useState(null);
  const hold = m.status === "open";
  const go = async () => {
    try { await post({ op: "milestone_hold", id: m.id, hold }); setError(null); }
    catch (e) { setError(e.message); }
  };
  return html`<button class="btn" onClick=${go} title=${hold ? "keep its plans and tasks out of the queue; a running task finishes" : ""}>
    ${hold ? "Put on hold" : "Resume"}</button>${error ? html`<span class="err">${error}</span>` : null}`;
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

function pendingGroups(d, xs) {
  const items = xs.map((x) => ({ x, ...impact(d, x) }))
    .sort((a, b) => (b.stalls - a.stalls) || ((b.top ?? -1e9) - (a.top ?? -1e9)) || (b.waiting.length - a.waiting.length) || a.x.id - b.x.id);
  const groups = new Map();
  for (const it of items) {
    const k = it.milestone ? it.milestone.id : 0;
    (groups.get(k) || groups.set(k, []).get(k)).push(it);
  }
  return groups;
}

// The pending decision after x in the Decisions list (sign-offs, then questions; or newest first), else the first other one; null if none.
const newestFirst = (xs) => [...xs].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id);
function nextPending(d, x, newest) {
  const pending = d.decisions.filter((y) => y.status === "pending");
  const ids = newest ? newestFirst(pending).map((y) => y.id) : [pending.filter((y) => y.signoff), pending.filter((y) => !y.signoff)]
    .flatMap((xs) => [...pendingGroups(d, xs).values()].flat().map((it) => it.x.id));
  const i = ids.indexOf(x.id);
  return ids.slice(i + 1).concat(ids.slice(0, Math.max(i, 0))).find((id) => id !== x.id) ?? null;
}

// Every pending decision, newest first, with what the milestone grouping would show inline.
function PendingNewest({ d, xs }) {
  return html`<div class="tablewrap"><table><tbody>${newestFirst(xs).map((x) => {
    const { waiting, top, stalls } = impact(d, x);
    const ms = [...new Set(waiting.map((t) => d.milestoneOfTask(t)).filter(Boolean))];
    const by = x.created_by_task != null ? d.task.get(x.created_by_task) : null;
    return html`<tr>
      <td style="width:56px"><${Id} kind="decision" id=${x.id} /></td>
      <td class="subject"><a href=${`#/decision/${x.id}?view=newest`}>${x.subject}</a> ${x.signoff ? html`<span class="tag signoff">${x.signoff}</span>` : null}
        ${stalls ? html` <span class="tag signoff" title="its tasks have a higher priority than the head of the queue">would run next</span>` : null}
        ${x.intro ? html`<div class="intro-line">${x.intro}</div>` : null}
        ${x.options.length ? html`<div class="muted" style="font-size:12px">${x.options.join(" / ")}</div>` : null}
        <div class="ctx">${ms.length ? html`<span>${ms.map((m, i) => html`${i ? ", " : ""}<${NamedLink} kind="milestone" r=${m} />`)}</span>` : null}
          ${waiting.length ? html`<span>holds up ${waiting.map((t, i) => html`${i ? ", " : ""}<${TaskLink} t=${t} />`)} (prio ${top})</span>`
            : html`<span class="muted">holds up nothing</span>`}
          ${by ? html`<span class="muted">asked by <${TaskLink} t=${by} /></span>` : null}</div></td>
      <td class="muted" style="width:110px"><${Time} ts=${x.created_at} /></td></tr>`;
  })}</tbody></table></div>`;
}

function PendingGroups({ d, xs }) {
  const groups = pendingGroups(d, xs);
  return html`${[...groups].map(([k, its]) => html`<h3>${k ? html`<${NamedLink} kind="milestone" r=${d.milestone.get(k)} />` : "No open task waits"}</h3>
    <div class="tablewrap"><table><tbody>${its.map(({ x, waiting, top, stalls }) => html`<tr>
      <td style="width:56px"><${Id} kind="decision" id=${x.id} /></td>
      <td class="subject"><a href=${`#/decision/${x.id}`}>${x.subject}</a> ${x.signoff ? html`<span class="tag signoff">${x.signoff}</span>` : null}
        ${x.intro ? html`<div class="intro-line">${x.intro}</div>` : null}
        ${x.options.length ? html`<div class="muted" style="font-size:12px">${x.options.join(" / ")}</div>` : null}</td>
      <td style="width:200px">${waiting.length ? html`holds up ${waiting.length} task${waiting.length > 1 ? "s" : ""}, prio ${top}` : html`<span class="muted">holds up nothing</span>`}
        ${stalls ? html`<div><span class="tag signoff" title="its tasks have a higher priority than the head of the queue">would run next</span></div>` : null}</td>
      <td class="muted" style="width:110px"><${Time} ts=${x.created_at} /></td></tr>`)}</tbody></table></div>`)}`;
}

// once, decided by a worker, superseded: what keeps a closed decision out of the log or marks it.
function DecisionTags({ d, x }) {
  const by = d.supersededBy.get(x.id) || [];
  return html`${x.kind === "once" && !x.signoff ? html` <span class="tag">once</span>` : null}
    ${x.decided_by_task != null ? html` <span class="tag">worker's choice</span>` : null}
    ${by.length ? html` <span class="tag">${by.some((s) => !s.note) ? "superseded" : "partly superseded"} by ${by.map((s) => `d${s.id}`).join(", ")}</span>` : null}`;
}

export function Decisions({ d, query }) {
  const [filter, setFilter] = useState("log");
  const q = new URLSearchParams(query || "");
  const mid = Number(q.get("milestone")) || null;
  const newest = q.get("view") === "newest";
  const ms = mid ? d.milestone.get(mid) : null;
  if (ms) {
    const xs = d.milestoneDecisions(ms);
    return html`<div class="crumbs"><a href="#/decisions">Decisions</a></div>
      <h1>Pending decisions for <${NamedLink} kind="milestone" r=${ms} /></h1>
      <div class="sub">The ones its open tasks wait for: the milestone can't finish until they are answered. <a href="#/decisions">All decisions</a></div>
      ${xs.length ? html`<${PendingGroups} d=${d} xs=${xs} />` : html`<div class="empty">None pending.</div>`}`;
  }
  const pending = d.decisions.filter((x) => x.status === "pending");
  const signoffs = pending.filter((x) => x.signoff), questions = pending.filter((x) => !x.signoff);
  const allClosed = d.decisions.filter((x) => x.status !== "pending")
    .sort((a, b) => (b.closed_at || "").localeCompare(a.closed_at || "") || b.id - a.id);
  const closed = filter === "log" ? allClosed.filter(d.inLog) : allClosed;
  const list = (xs) => html`<div class="tablewrap"><table><tbody>${xs.map((x) => html`<tr>
    <td style="width:56px"><${Id} kind="decision" id=${x.id} /></td>
    <td class="subject"><a href=${`#/decision/${x.id}`}>${x.subject}</a> ${x.signoff ? html`<span class="tag signoff">${x.signoff}</span>` : null}
      <${DecisionTags} d=${d} x=${x} />
      ${x.status !== "pending" ? html`<div class="muted" style="font-size:12px"><${InlineMd} text=${x.answer || x.reason} /></div>` : null}</td>
    <td style="width:110px"><${Pill} status=${x.status} /></td>
    <td class="muted" style="width:140px">${(d.tasksWaiting.get(x.id) || []).length} task(s)</td>
    <td class="muted" style="width:110px"><${Time} ts=${x.closed_at || x.created_at} /></td></tr>`)}</tbody></table></div>`;
  return html`<h1>Decisions</h1>
    <div class="sub">Answer one on its page, in a Claude session, with <code>pm decide</code> or <code>pm decision answer</code>; <a href="#/reports">an executive summary</a> tells what changed.</div>
    <div class="tabs"><a class=${"tab" + (newest ? "" : " on")} href="#/decisions">By milestone</a>
      <a class=${"tab" + (newest ? " on" : "")} href="#/decisions?view=newest">Newest first</a></div>
    ${newest ? html`<h2>Pending (${pending.length})</h2>${pending.length ? html`<${PendingNewest} d=${d} xs=${pending} />` : html`<div class="empty">None pending.</div>`}`
      : html`${signoffs.length ? html`<h2>Sign-offs (${signoffs.length})</h2><${PendingGroups} d=${d} xs=${signoffs} />` : null}
    <h2>Questions (${questions.length})</h2>${questions.length ? html`<${PendingGroups} d=${d} xs=${questions} />` : html`<div class="empty">None pending.</div>`}`}
    <h2>Closed (${closed.length})</h2>
    <${Chips} value=${filter} set=${setFilter} options=${[["log", "decision log"], ["all", "all closed"]]} />
    ${filter === "log" ? html`<div class="sub">Answered lasting decisions nothing fully supersedes, newest first: <code>pm decision list --answered</code>.</div>` : null}
    ${closed.length ? list(closed) : html`<div class="empty">None yet.</div>`}`;
}

const KNOWN = new Set(["id", "subject", "status", "options", "body", "intro", "answer", "reason", "created_at", "closed_at",
  "created_by_task", "signoff", "signoff_view", "kind", "decided_by_task", "consequences"]);

const links = (d, xs, verb) => xs.map((s) => html`<div class="sub">${(s.note ? "Partly " + verb.toLowerCase() : verb) + " "}<a href=${`#/decision/${s.id}`}>decision ${s.id}: ${(d.decision.get(s.id) || {}).subject}</a>${s.note ? ` (${s.note})` : ""}</div>`);

// pm appends each entry as "<date>[, task N]: <text>"; a worker sometimes repeats that head in its text.
const HEAD = /^(\d{4}-\d{2}-\d{2})(?:, task (\d+))?: /;
function Consequences({ text }) {
  const entries = text.split(/\n\n(?=\d{4}-\d{2}-\d{2}(?:, task \d+)?: )/).map((e) => {
    const m = e.match(HEAD);
    if (!m) return { body: e };
    let body = e.slice(m[0].length);
    while (body.startsWith(m[0])) body = body.slice(m[0].length);
    return { date: m[1], task: m[2], body };
  });
  return html`<h3>Consequences</h3>${entries.map((e) => html`<div class="card conseq">
    ${e.date ? html`<div class="sub">${e.date}${e.task ? html`, <a href=${`#/task/${e.task}`}>task ${e.task}</a>` : null}</div>` : null}
    <${Markdown} text=${e.body} /></div>`)}`;
}

export function Decision({ d, id, query }) {
  const newest = new URLSearchParams(query || "").get("view") === "newest";
  const x = d.decision.get(id);
  if (!x) return html`<div class="empty">No decision ${id}.</div>`;
  const waiting = d.tasksWaiting.get(x.id) || [];
  const extra = Object.entries(x).filter(([k, v]) => !KNOWN.has(k) && v != null);
  const pending = x.status === "pending";
  return html`<div class="crumbs"><a href=${newest ? "#/decisions?view=newest" : "#/decisions"}>Decisions</a></div>
    <h1>${x.subject}</h1>
    <div class="row sub"><${Pill} status=${x.status} />${x.signoff ? html`<span class="tag signoff">sign-off: ${x.signoff}</span>` : null}
      <span>decision ${x.id} · created ${stamp(x.created_at)}</span>
      ${x.created_by_task != null ? html`<span>by <a href=${`#/task/${x.created_by_task}`}>task ${x.created_by_task}</a></span>` : null}
      ${!x.signoff ? html`<span class="tag">${x.kind === "once" ? "once: not in the log" : "rule"}</span>` : null}
      ${extra.map(([k, v]) => html`<span class="tag">${k}: ${String(v)}</span>`)}</div>
    ${links(d, d.supersededBy.get(x.id) || [], "Superseded by")}${links(d, d.supersedes.get(x.id) || [], "Supersedes")}
    <${Intro} key=${x.id} intro=${x.intro}>${x.body.trim() || !x.signoff_view ? html`<div class="card"><${Markdown} text=${x.body} empty="No body." /></div>` : null}</${Intro}>
    ${x.status !== "pending" ? html`<h3>${x.status === "answered" ? "Answer" : "Irrelevant"}</h3>
      <div class="sub">${stamp(x.closed_at)}${x.decided_by_task != null ? html`, the own choice of <a href=${`#/task/${x.decided_by_task}`}>task ${x.decided_by_task}</a>` : null}</div>
      <div class="card"><${Markdown} text=${x.answer || x.reason} /></div>` : null}
    ${x.consequences ? html`<${Consequences} text=${x.consequences} />` : null}
    ${x.options.length && !pending ? html`<h3>Options</h3><ol class="options">${x.options.map((o, i) => html`<li class=${i === 0 ? "rec" : ""}><${InlineMd} text=${o} /></li>`)}</ol>` : null}
    ${x.signoff_view ? html`<h2>What it approves</h2><div class="card"><${Markdown} text=${x.signoff_view} /></div>` : null}
    ${pending ? html`<${Resolve} key=${x.id} d=${d} x=${x} next=${nextPending(d, x, newest)} view=${newest ? "?view=newest" : ""} />` : null}
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
  const byMilestone = d.milestones.map((m) => ({ label: `m${m.id} ` + (m.subject.length > 16 ? m.subject.slice(0, 15) + "…" : m.subject),
    value: d.cost(d.milestoneTasks(m).flatMap(d.taskRuns)) })).filter((x) => x.value > 0);
  const closed = d.tasks.filter((t) => t.status !== "open").map(d.taskRuns).filter((rs) => rs.length).map(d.cost).sort((a, b) => a - b);
  const edges = [0.1, 0.25, 0.5, 1, 2, 5, 10, 20];
  const byCost = [0, ...edges].map((lo, i) => {
    const hi = edges[i], cs = closed.filter((c) => c >= lo && (hi === undefined || c < hi));
    return { label: hi === undefined ? `≥ ${usd(lo)}` : `${usd(lo)}–${usd(hi)}`, short: hi === undefined ? `${usd(lo)}+` : `<${usd(hi)}`,
      value: cs.length, extra: `${usd(cs.reduce((s, c) => s + c, 0))} in total` };
  });
  const median = closed.length ? (closed[(closed.length - 1) >> 1] + closed[closed.length >> 1]) / 2 : 0;
  const tok = finished.reduce((s, r) => s + d.tokens(r), 0);
  const cacheRead = finished.reduce((s, r) => s + (r.cache_read_tokens || 0), 0);
  const secs = finished.reduce((s, r) => s + seconds(r.started_at, r.ended_at), 0);
  const shown = res === "all" ? d.runs : d.runs.filter((r) => r.result === res);
  // Warm hand-off: done work runs that resumed a session against those that started fresh.
  const work = finished.filter((r) => r.result === "done" && kindOf(r) === "work");
  const resumed = work.filter((r) => r.continues_run != null), fresh = work.filter((r) => r.continues_run == null);
  const avg = (rs) => (rs.length ? d.cost(rs) / rs.length : 0);
  // Per task kind and model: runs, failures (a warm hand-off's failed resume aside), cost.
  const byModel = new Map();
  for (const r of finished.filter((r) => r.result !== "limit")) {
    const k = `${kindOf(r)}\t${r.model || "unknown"}`;
    (byModel.get(k) || byModel.set(k, []).get(k)).push(r);
  }
  const modelRows = [...byModel].map(([k, rs]) => {
    const [kind, model] = k.split("\t");
    const failed = rs.filter((r) => r.result === "failed" && !(r.reason || "").startsWith("resuming the session failed")).length;
    return { kind, model, n: rs.length, failed, cost: d.cost(rs) };
  }).sort((a, b) => KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind) || b.cost - a.cost);
  return html`<h1>Runs and cost</h1>
    <div class="tiles">
      <${Tile} label="Cost" value=${usd(d.cost(finished))} sub=${`${finished.length} runs`} />
      <${Tile} label="Worker time" value=${duration(secs)} />
      <${Tile} label="Tokens" value=${tokens(tok) || "0"} sub=${tok ? `${Math.round((100 * cacheRead) / tok)}% cache reads` : ""} />
      <${Tile} label="Failed" value=${finished.filter((r) => r.result === "failed").length}
        sub=${`${finished.filter((r) => r.result === "limit").length} usage limits`} />
      <${Tile} label="Warm hand-off" value=${resumed.length ? usd(avg(resumed)) : "–"}
        sub=${resumed.length ? `per resumed work run (${resumed.length}); fresh: ${usd(avg(fresh))} (${fresh.length})` : "no resumed run yet"} />
    </div>
    <div class="card"><b>Cost per day</b><${Bars} data=${series} fmt=${usd} /></div>
    <div class="grid2" style="margin-top:12px">
      <div class="card"><b>Cost per task kind</b><${Bars} data=${byKind} fmt=${usd} horizontal /></div>
      <div class="card"><b>Cost per milestone</b><${Bars} data=${byMilestone} fmt=${usd} horizontal /></div>
    </div>
    <h2>Per task kind and model</h2>
    ${modelRows.length ? html`<div class="tablewrap"><table class="compact">
      <thead><tr><th>kind</th><th>model</th><th class="r">runs</th><th class="r">failed</th><th class="r">cost per run</th><th class="r">cost</th></tr></thead>
      <tbody>${modelRows.map((x) => html`<tr><td>${x.kind}</td><td class="mono">${x.model}</td><td class="r num">${x.n}</td>
        <td class="r num">${x.failed ? `${x.failed} (${Math.round((100 * x.failed) / x.n)}%)` : "0"}</td>
        <td class="r num">${usd(x.cost / x.n)}</td><td class="r num">${usd(x.cost)}</td></tr>`)}</tbody></table></div>`
      : html`<div class="empty">No runs yet.</div>`}
    <div class="card" style="margin-top:12px"><b>Tasks per cost bracket</b>
      <span class="muted"> · ${closed.length} done or dropped tasks with runs, cost over all their runs${closed.length ? `, median ${usd(median)}` : ""}</span>
      <${Bars} data=${closed.length ? byCost : []} fmt=${(n) => (Number.isInteger(n) ? String(n) : "")} /></div>
    <h2>All runs</h2>
    <${Chips} value=${res} set=${setRes} options=${[["all", "all"], ...["running", "done", "blocked", "failed", "limit"].map((k) => [k, k])]} />
    <${RunsTable} d=${d} runs=${shown} showTask />`;
}
