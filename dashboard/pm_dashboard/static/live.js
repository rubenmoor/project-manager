// The run view: left, one timeline (queue above, highest priority nearest; the running task between two rules; retired
// tasks below, newest first); right, the selected task: the running one's output, an open one's details, a retired
// one's outcome. A click selects (outline); j/k move the selection.
import { html, useState, useEffect, useRef, useLayoutEffect } from "/vendor/preact-htm.js";
import { Pill, Kind, Prio, Elapsed, Time, Markdown, Ref, Named, NamedLink } from "/ui.js";
import { Log } from "/views.js";
import { usd, stamp, duration, seconds, tokens } from "/data.js";

function useHeight(ref) {
  const [h, setH] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const o = new ResizeObserver(([e]) => setH(e.contentRect.height));
    o.observe(ref.current);
    return () => o.disconnect();
  }, []);
  return h;
}

function Item({ id, sel, select, cls = "", children }) {
  return html`<div class=${`tl-item ${cls}${sel === id ? " sel" : ""}`} role="button" tabindex="0" data-task=${id}
    onClick=${() => select(id)} onKeyDown=${(e) => e.key === "Enter" && select(id)}>${children}</div>`;
}

const crumbs = (d, t) => {
  const m = d.milestoneOfTask(t);
  return html`<div class="tl-detail-parents">
    <span class="muted">milestone</span><${NamedLink} kind="milestone" r=${m} />
    <span class="muted">plan</span><${NamedLink} kind="plan" r=${d.plan.get(t.plan_id)} /></div>`;
};

function Runs({ d, runs }) {
  if (!runs.length) return null;
  return html`<h3>Runs</h3><table class="compact"><tbody>${[...runs].reverse().map((r) => html`<tr>
    <td><${Pill} status=${r.result} /></td><td class="num">${stamp(r.started_at)}</td>
    <td class="num r"><${Elapsed} start=${r.started_at} end=${r.ended_at} /></td>
    <td class="num r">${r.turns != null ? `${r.turns} turns` : ""}</td><td class="num r">${tokens(d.tokens(r))}</td>
    <td class="num r">${usd(r.cost_usd)}</td></tr>
    ${r.reason ? html`<tr><td></td><td colspan="5" class="muted reason">${r.reason.split("\n")[0].slice(0, 200)}</td></tr>` : null}`)}</tbody></table>`;
}

function Files({ t }) {
  const [c, setC] = useState(null);
  useEffect(() => {
    let gone = false;
    setC(null);
    fetch(`/api/task/${t.id}/commit`).then((r) => r.json()).then((j) => !gone && setC(j));
    return () => { gone = true; };
  }, [t.id, t.retired_at]);
  if (!c) return null;
  if (!c.commit) return html`<h3>Changes</h3><div class="muted">No commit by <code>pm run</code>.</div>`;
  return html`<h3>Changes <span class="muted mono">${c.commit.slice(0, 7)}</span></h3>
    <table class="compact"><tbody>${c.files.map((f) => html`<tr><td class="mono">${f.file}</td>
      <td class="num r plus">${f.added != null ? `+${f.added}` : ""}</td><td class="num r minus">${f.deleted != null ? `−${f.deleted}` : "bin"}</td></tr>`)}</tbody></table>
    <div><a href=${`#/task/${t.id}`}>the diff on the task page</a></div>`;
}

function Detail({ d, t, running }) {
  const runs = d.taskRuns(t);
  const qi = running ? -1 : d.queue.filter((id) => !d.running || id !== d.running.task_id).indexOf(t.id);
  const why = d.excluded(t);
  const decs = d.decisionsOf.get(t.id) || [];
  const last = runs.filter((r) => r.ended_at).pop();
  const head = html`<div class="live-head">
    ${crumbs(d, t)}
    <div class="row"><span class="grow tl-detail-subject"><${Named} kind="task" r=${t} /></span>
      <a class="btn" href=${`#/task/${t.id}`}>Task page →</a></div>
    <div class="row sub">${running ? html`<${Pill} status="running" />` : html`<${Pill} status=${d.taskState(t)} />`}<${Kind} kind=${t.kind} />
      <span>task ${t.id} · priority <${Prio} p=${d.prio(t)} /></span>
      ${running ? html`<span>· <${Elapsed} start=${d.running.started_at} /></span>` : null}
      ${qi >= 0 ? html`<span>· ${qi === 0 ? "next in the queue" : `#${qi + 1} in the queue`}</span>` : null}</div></div>`;
  const body = html`<div class="card live-body"><${Markdown} text=${t.body} /></div>`;
  if (running) return html`${head}${body}<${Log} d=${d} t=${t} live full />`;
  const fail = last && last.result === "failed" ? last : null;
  if (t.status === "open") return html`${head}<div class="live-detail">
    ${fail ? html`<div class="banner error"><b>Run ${fail.id} failed</b> <span class="muted">${stamp(fail.ended_at)}</span><pre>${fail.reason || ""}</pre></div>` : null}
    ${why ? html`<div class="banner warn">${typeof why === "string" ? `Not queued: ${why}.` : html`Waits for ${why.map((x, i) => html`${i ? ", " : ""}<a href=${`#/decision/${x.id}`}>decision ${x.id}: ${x.subject}</a>`)}.`}</div>` : null}
    ${body}
    ${decs.length ? html`<h3>Decisions</h3><ul class="plain">${decs.map((x) => html`<li><${Pill} status=${x.status} /> <a href=${`#/decision/${x.id}`}>${x.subject}</a></li>`)}</ul>` : null}
    <div class="muted" style="margin-top:10px">created ${stamp(t.created_at)}${t.created_by_task != null ? html` by <a href=${`#/task/${t.created_by_task}`}>task ${t.created_by_task}</a>` : ""}</div>
    <${Runs} d=${d} runs=${runs} />
    ${fail ? html`<h3>Output of the failed run</h3><${Log} d=${d} t=${t} live=${false} run=${fail.id} />` : null}
  </div>`;
  const total = runs.reduce((s, r) => s + seconds(r.started_at, r.ended_at), 0);
  return html`${head}<div class="live-detail">
    ${body}
    <div class="card outcome"><div class="row"><b>${t.status === "done" ? "Done" : "Dropped"}</b>
      <span class="muted">${stamp(t.retired_at)} · <${Time} ts=${t.retired_at} /></span>
      ${runs.length ? html`<span class="muted">· ${runs.length} run${runs.length > 1 ? "s" : ""}, ${duration(total)}, ${usd(d.cost(runs))}</span>` : null}</div>
      ${t.note ? html`<div class="note">${t.note}</div>` : null}</div>
    <${Files} t=${t} />
    <${Runs} d=${d} runs=${runs} />
    ${last ? html`<h3>Output of the last run</h3><${Log} d=${d} t=${t} live=${false} />` : null}
  </div>`;
}

export function Live({ d }) {
  const r = d.running;
  const rt = r ? d.task.get(r.task_id) : null;
  // The worker closed its task (or made it wait for a decision) but hasn't exited yet.
  const wrapping = rt && (rt.status !== "open" || d.blockers(rt).length > 0);
  const current = wrapping ? null : rt;
  const queue = d.queue.filter((id) => !current || id !== current.id).map((id) => d.task.get(id)).reverse();
  const retired = d.tasks.filter((t) => t.retired_at).sort((a, b) => (a.retired_at < b.retired_at ? 1 : a.retired_at > b.retired_at ? -1 : b.id - a.id));
  const lastRun = [...d.runs].filter((x) => x.ended_at).sort((a, b) => (a.ended_at < b.ended_at ? 1 : -1))[0];
  const failedLast = (t) => { const e = d.taskRuns(t).filter((x) => x.ended_at).pop(); return e && e.result === "failed"; };
  const alive = d.state.pm_run != null;
  const between = !r && alive && lastRun ? lastRun : null;
  const stopped = !r && !alive && lastRun && lastRun.result === "failed" ? lastRun : null;
  const next = d.queue.length ? d.task.get(d.queue[0]) : null;
  const busy = wrapping ? { spin: true, text: `Task ${rt.id} is ${rt.status === "open" ? "waiting for a decision" : rt.status}; its worker is finishing`,
      sub: "Then pm records the run, commits, and starts the next task." }
    : between && between.result === "limit" ? { spin: false, text: "pm run waits for the usage limit to reset",
      sub: (between.reason || "").split("\n")[0] + "; then task " + between.task_id + " again." }
    : between ? { spin: true, text: `Committing task ${between.task_id}, picking the next task`,
      sub: next ? `Next in the queue: t${next.id} ${next.subject}` : "The queue is empty: pm run stops." }
    : null;
  const [picked, setPicked] = useState(null);
  const sel = picked != null && d.task.get(picked) ? picked : current ? current.id : lastRun ? lastRun.task_id : null;
  const order = [...queue.map((t) => t.id), ...(current ? [current.id] : []), ...retired.map((t) => t.id)];
  const scroller = useRef(null), card = useRef(null), top = useRef(null), bottom = useRef(null);
  const H = useHeight(scroller), cardH = useHeight(card), topH = useHeight(top), bottomH = useHeight(bottom);
  // Room so the current task can reach both the top and the bottom of the column.
  const padTop = Math.max(0, H - cardH - topH), padBottom = Math.max(0, H - cardH - bottomH);
  const centred = useRef(null);
  useLayoutEffect(() => {
    const key = current ? current.id : "none";
    if (!H || !cardH || centred.current === key) return;
    centred.current = key;
    scroller.current.scrollTop = card.current.offsetTop - scroller.current.offsetTop - (H - cardH) / 2;
  }, [H, cardH, current && current.id]);
  useEffect(() => {
    const k = (e) => {
      if (/INPUT|TEXTAREA/.test(document.activeElement.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key !== "j" && e.key !== "k") return;
      const i = order.indexOf(sel);
      const next = order[Math.max(0, Math.min(order.length - 1, i + (e.key === "j" ? 1 : -1)))];
      if (next == null) return;
      setPicked(next);
      const el = scroller.current.querySelector(`[data-task="${next}"]`);
      if (el) el.scrollIntoView({ block: "nearest" });
    };
    addEventListener("keydown", k);
    return () => removeEventListener("keydown", k);
  }, [sel, order.join()]);
  const selTask = sel != null ? d.task.get(sel) : null;
  return html`<div class="live">
    <div class="tl" ref=${scroller}>
      <div style=${`height:${padTop}px`}></div>
      <div ref=${top}>${queue.length ? html`<div class="tl-label">queue, next nearest</div>` : null}
        ${queue.map((t, i) => html`<${Item} id=${t.id} sel=${sel} select=${setPicked}>
          <span class="tl-pos num">${queue.length - i === 1 ? "next" : `#${queue.length - i}`}</span>
          <span class="tl-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} />${failedLast(t) ? html` <${Pill} status="failed" />` : null}</span><${Prio} p=${d.prio(t)} /></${Item}>`)}</div>
      <div class="tl-current" ref=${card}>
        ${current ? html`<${Item} id=${current.id} sel=${sel} select=${setPicked} cls="now">
            <div class="grow"><div class="row"><${Pill} status="running" /><span class="sub">task ${current.id} · <${Elapsed} start=${r.started_at} /></span></div>
            <div class="tl-current-subject"><${Named} kind="task" r=${current} /> <${Kind} kind=${current.kind} /></div>
            <div class="muted"><${Named} kind="plan" r=${d.plan.get(current.plan_id)} /> · <${Named} kind="milestone" r=${d.milestoneOfTask(current)} /></div></div></${Item}>`
          : busy ? html`<div class="tl-idle tl-busy">${busy.spin ? html`<span class="spinner"></span>` : null}<div>
            <div class="sub">${busy.text}</div><div class="muted">${busy.sub}</div></div></div>`
          : stopped ? html`<div class="tl-idle"><div class="sub"><code>pm run</code> stopped: <a href=${`#/task/${stopped.task_id}`} onClick=${(e) => { e.preventDefault(); setPicked(stopped.task_id); }}>task ${stopped.task_id} failed</a>.</div>
            <div class="muted">${(stopped.reason || "").split("\n").find((l) => l.startsWith("likely cause: ")) || (stopped.reason || "").split("\n")[0]}</div></div>`
          : html`<div class="tl-idle"><div class="sub">No worker is running${d.queue.length ? html`: start them with <code>pm run</code> in the project's terminal` : ""}.</div>
            <div class="muted">${d.queue.length ? "The next task is just above." : "The queue is empty."}</div></div>`}
      </div>
      <div ref=${bottom}>${retired.length ? html`<div class="tl-label">done and dropped, newest first</div>` : null}
        ${retired.map((t) => {
          const runs = d.taskRuns(t);
          return html`<${Item} id=${t.id} sel=${sel} select=${setPicked} cls="retired"><${Pill} status=${t.status} />
            <span class="tl-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} /></span>
            <span class="muted num" style="white-space:nowrap">${runs.length ? usd(d.cost(runs)) + " · " : ""}<${Time} ts=${t.retired_at} /></span></${Item}>`;
        })}</div>
      <div style=${`height:${padBottom}px`}></div>
    </div>
    <div class="live-out">${selTask ? html`<${Detail} d=${d} t=${selTask} running=${current && current.id === selTask.id} />`
      : html`<div class="empty">No run yet. Select a task on the left.</div>`}</div>
  </div>`;
}
