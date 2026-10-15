// The run view: left, one timeline (queue above, highest priority nearest; the running task between two rules; retired
// tasks below, newest first); right, the selected task as on its task page. A click selects (outline).
import { html, useState, useEffect, useRef, useLayoutEffect } from "/vendor/preact-htm.js";
import { Pill, Kind, Prio, Elapsed, Time, Named } from "/ui.js";
import { Task } from "/views.js";
import { usd } from "/data.js";

const SHOW_BLOCKED = "pm-live-blocked";
const loadShow = () => { try { return localStorage.getItem(SHOW_BLOCKED) === "1"; } catch { return false; } };

// The queue view's order (pm/__init__.py: queue), so blocked tasks sit where they would queue once unblocked.
const queueKey = (d, t) => {
  const p = t.plan_id != null ? d.plan.get(t.plan_id) : null, m = d.milestoneOfTask(t);
  return [m ? m.queue_index : 0, -(t.effective_priority ?? 0), m ? m.id : 0, p ? p.id : 0, t.id];
};
const byKey = (a, b) => { for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] - b[i]; return 0; };

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

export function Live({ d }) {
  const r = d.running;
  const rt = r ? d.task.get(r.task_id) : null;
  // The worker closed its task (or made it wait for a decision) but hasn't exited yet.
  const wrapping = rt && (rt.status !== "open" || d.blockers(rt).length > 0);
  const current = wrapping ? null : rt;
  const [showBlocked, setShowBlocked] = useState(loadShow);
  const toggleBlocked = (on) => { setShowBlocked(on); try { localStorage.setItem(SHOW_BLOCKED, on ? "1" : "0"); } catch {} };
  const blocked = d.tasks.filter((t) => t.status === "open" && !d.queuePos.has(t.id) && (!current || t.id !== current.id)
    && d.blockers(t).length && (t.kind === "exec" || d.milestoneOfTask(t)?.status === "open"));
  const queued = d.queue.filter((id) => !current || id !== current.id).map((id) => d.task.get(id));
  const queue = (showBlocked && blocked.length
    ? [...queued, ...blocked].map((t) => [queueKey(d, t), t]).sort((a, b) => byKey(a[0], b[0])).map(([, t]) => t)
    : queued).reverse();
  const qpos = new Map(queued.map((t, i) => [t.id, i + 1]));
  const retired = d.tasks.filter((t) => t.retired_at).sort((a, b) => (a.retired_at < b.retired_at ? 1 : a.retired_at > b.retired_at ? -1 : b.id - a.id));
  const lastRun = [...d.runs].filter((x) => x.ended_at).sort((a, b) => (a.ended_at < b.ended_at ? 1 : -1))[0];
  const failedLast = (t) => { const e = d.taskRuns(t).filter((x) => x.ended_at).pop(); return e && e.result === "failed"; };
  const alive = d.state.pm_run != null;
  const between = !r && alive && lastRun ? lastRun : null;
  const crashed = !alive && (d.stale || d.crash) ? { run: d.stale, error: d.crash ? d.crash.error : null, at: d.crash ? d.crash.at : null } : null;
  const stopped = !r && !alive && !crashed && lastRun && lastRun.result === "failed" ? lastRun : null;
  const next = d.queue.length ? d.task.get(d.queue[0]) : null;
  const busy = wrapping ? { spin: true, text: `Task ${rt.id} is ${rt.status === "open" ? "waiting for a decision" : rt.status}; its worker is finishing`,
      sub: "Then pm records the run, commits, and starts the next task." }
    : between && between.result === "limit" ? { spin: false, text: "pm run waits for the usage limit to reset",
      sub: (between.reason || "").split("\n")[0] + "; then task " + between.task_id + " again." }
    : between ? { spin: true, text: `Committing task ${between.task_id}, picking the next task`,
      sub: next ? `Next in the queue: t${next.id} ${next.subject}` : "The queue is empty: pm run stops." }
    : null;
  const [picked, setPicked] = useState(null);
  const sel = picked != null && d.task.get(picked) ? picked : current ? current.id : crashed && crashed.run ? crashed.run.task_id : lastRun ? lastRun.task_id : null;
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
  const selTask = sel != null ? d.task.get(sel) : null;
  return html`<div class="live">
    <div class="tl" ref=${scroller}>
      <label class="tl-toggle"><input type="checkbox" checked=${showBlocked} onChange=${(e) => toggleBlocked(e.target.checked)} />
        show tasks blocked by decisions${blocked.length ? html` <span class="muted num">(${blocked.length})</span>` : null}</label>
      <div style=${`height:${padTop}px`}></div>
      <div ref=${top}>${queue.length ? html`<div class="tl-label">queue, next nearest${showBlocked && blocked.length ? "; blocked tasks where they would queue" : ""}</div>` : null}
        ${queue.map((t) => {
          const q = qpos.get(t.id);
          if (q == null) return html`<${Item} id=${t.id} sel=${sel} select=${setPicked} cls="blocked">
            <span class="tl-pos num" title="waits for a pending decision">—</span>
            <span class="tl-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} />
              <span class="tag waits">waits for ${d.blockers(t).map((x) => `d${x.id}`).join(", ")}</span></span><${Prio} p=${d.prio(t)} /></${Item}>`;
          return html`<${Item} id=${t.id} sel=${sel} select=${setPicked}>
            <span class="tl-pos num">${q === 1 ? "next" : `#${q}`}</span>
            <span class="tl-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} />${failedLast(t) ? html` <${Pill} status="failed" />` : null}</span><${Prio} p=${d.prio(t)} /></${Item}>`;
        })}</div>
      <div class="tl-current" ref=${card}>
        ${current ? html`<${Item} id=${current.id} sel=${sel} select=${setPicked} cls="now">
            <div class="grow"><div class="row"><${Pill} status="running" /><span class="sub">task ${current.id} · <${Elapsed} start=${r.started_at} /></span></div>
            <div class="tl-current-subject"><${Named} kind="task" r=${current} /> <${Kind} kind=${current.kind} /></div>
            <div class="muted">${current.plan_id != null ? html`<${Named} kind="plan" r=${d.plan.get(current.plan_id)} /> · <${Named} kind="milestone" r=${d.milestoneOfTask(current)} />` : "executive intervention"}</div></div></${Item}>`
          : busy ? html`<div class="tl-idle tl-busy">${busy.spin ? html`<span class="spinner"></span>` : null}<div>
            <div class="sub">${busy.text}</div><div class="muted">${busy.sub}</div></div></div>`
          : crashed ? html`<div class="tl-idle tl-crashed"><div class="sub"><b>pm not running</b>: <code>pm run</code> crashed${crashed.run ? html` during <a href=${`#/task/${crashed.run.task_id}`} onClick=${(e) => { e.preventDefault(); setPicked(crashed.run.task_id); }}>task ${crashed.run.task_id}</a>` : ""}${crashed.at ? html`, <${Time} ts=${crashed.at} />` : ""}.</div>
            ${crashed.error ? html`<pre class="crash">${crashed.error}</pre>` : html`<div class="muted">It left no error message (killed, or out of memory?).</div>`}
            <div class="muted">Fix the cause, then start it again with <code>pm run</code>${crashed.run ? "; it records the run as failed" : ""}.</div></div>`
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
    <div class="live-out">${selTask ? html`<${Task} key=${selTask.id} d=${d} id=${selTask.id} embedded />`
      : html`<div class="empty">No run yet. Select a task on the left.</div>`}</div>
  </div>`;
}
