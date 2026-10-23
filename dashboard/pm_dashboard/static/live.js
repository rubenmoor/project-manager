// The run view: left, one timeline (queue above, highest priority nearest; the running tasks, one per work tree, stacked
// between two rules; retired
// tasks below, newest first); right, the selected task as on its task page. A click selects (outline).
import { html, useState, useEffect, useRef, useLayoutEffect } from "/vendor/preact-htm.js";
import { Pill, Kind, Worktree, Prio, Elapsed, Time, Named } from "/ui.js";
import { Task, Handoff } from "/views.js";
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

// A task that changed place (running → done, next → running): a ghost box morphs from its old place to its new one;
// done rows already shown slide down to make room. Nothing on the first render or with reduced motion.
function useMoves(root) {
  const prev = useRef(null);
  useLayoutEffect(() => {
    const els = [...root.current.querySelectorAll("[data-task][data-zone]")];
    const now = new Map(els.map((el) => [el.dataset.task, { el, zone: el.dataset.zone, rect: el.getBoundingClientRect() }]));
    const old = prev.current;
    prev.current = now;
    if (!old || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let pushed = false;
    for (const [id, n] of now) {
      const o = old.get(id);
      if (!o || o.zone === n.zone) continue;
      const pane = n.el.closest(".tl-pane, .tl-current").getBoundingClientRect();
      if (n.rect.bottom < pane.top || n.rect.top > pane.bottom) continue;
      if (n.zone === "retired") pushed = true;
      const ghost = document.createElement("div");
      ghost.className = "tl-ghost";
      document.body.append(ghost);
      const box = (r) => ({ left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
      ghost.animate([{ ...box(o.rect), opacity: 1 }, { ...box(n.rect), opacity: 0.9, offset: 0.8 }, { ...box(n.rect), opacity: 0 }],
        { duration: 500, easing: "cubic-bezier(.3,.7,.3,1)" }).finished.then(() => ghost.remove(), () => ghost.remove());
      n.el.animate([{ opacity: 0 }, { opacity: 0, offset: 0.6 }, { opacity: 1 }], { duration: 500 });
      if (n.zone === "retired") { n.el.classList.add("just-done"); n.el.addEventListener("animationend", () => n.el.classList.remove("just-done"), { once: true }); }
    }
    if (pushed) for (const [id, n] of now) {
      const o = old.get(id);
      if (o && o.zone === "retired" && n.zone === "retired" && o.rect.top !== n.rect.top)
        n.el.animate([{ transform: `translateY(${o.rect.top - n.rect.top}px)` }, { transform: "none" }], { duration: 400, easing: "ease-out" });
    }
  });
}

function Item({ id, sel, select, cls = "", zone, children }) {
  return html`<div class=${`tl-item ${cls}${sel === id ? " sel" : ""}`} role="button" tabindex="0" data-task=${id} data-zone=${zone}
    onClick=${() => select(id)} onKeyDown=${(e) => e.key === "Enter" && select(id)}>${children}</div>`;
}

export function Live({ d }) {
  const r = d.running;
  // Per running run: its task; wrapping: the worker closed it (or made it wait for a decision) but hasn't exited yet.
  const now = d.runningAll.map((x) => { const t = d.task.get(x.task_id); return { r: x, t, wrapping: t.status !== "open" || d.blockers(t).length > 0 || d.unmet(t).length > 0 }; });
  const currents = now.filter((x) => !x.wrapping);
  const current = currents.length ? currents[0].t : null;
  const isCurrent = (id) => currents.some((x) => x.t.id === id);
  const [showBlocked, setShowBlocked] = useState(loadShow);
  const toggleBlocked = (on) => { setShowBlocked(on); try { localStorage.setItem(SHOW_BLOCKED, on ? "1" : "0"); } catch {} };
  const blocked = d.tasks.filter((t) => t.status === "open" && !d.queuePos.has(t.id) && !isCurrent(t.id)
    && (d.blockers(t).length || d.unmet(t).length) && (t.kind === "exec" || d.milestoneOfTask(t)?.status === "open"));
  const queued = d.queue.filter((id) => !isCurrent(id)).map((id) => d.task.get(id));
  const queue = (showBlocked && blocked.length
    ? [...queued, ...blocked].map((t) => [queueKey(d, t), t]).sort((a, b) => byKey(a[0], b[0])).map(([, t]) => t)
    : [...queued]).reverse();
  const qpos = new Map(queued.map((t, i) => [t.id, i + 1]));
  const retired = d.tasks.filter((t) => t.retired_at).sort((a, b) => (a.retired_at < b.retired_at ? 1 : a.retired_at > b.retired_at ? -1 : b.id - a.id));
  const lastRun = [...d.runs].filter((x) => x.ended_at).sort((a, b) => (a.ended_at < b.ended_at ? 1 : -1))[0];
  const failedLast = (t) => { const e = d.taskRuns(t).filter((x) => x.ended_at).pop(); return e && e.result === "failed"; };
  const alive = d.state.pm_run != null;
  const between = !r && alive && lastRun ? lastRun : null;
  const crashed = !alive && (d.stale || d.crash) ? { run: d.stale, error: d.crash ? d.crash.error : null, at: d.crash ? d.crash.at : null } : null;
  const stopped = !r && !alive && !crashed && lastRun && lastRun.result === "failed" ? lastRun : null;
  const next = d.queue.length ? d.task.get(d.queue[0]) : null;
  const wrapText = (t) => ({ spin: true, text: `Task ${t.id} is ${t.status === "open" ? "waiting" : t.status}; its worker is finishing`,
      sub: "Then pm records the run, commits, and starts the next task." });
  const busy = between && between.result === "limit" ? { spin: false, text: "pm run waits for the usage limit to reset",
      sub: (between.reason || "").split("\n")[0] + "; then task " + between.task_id + " again." }
    : !r && alive && !next ? { spin: false, text: "pm run waits for tasks",
      sub: "The queue is empty; it starts when a task is queued or a decision answered." }
    : between ? { spin: true, text: `Committing task ${between.task_id}, picking the next task`,
      sub: next ? `Next in the queue: t${next.id} ${next.subject}` : "" }
    : null;
  const [picked, setPicked] = useState(null);
  const sel = picked != null && d.task.get(picked) ? picked : current ? current.id : crashed && crashed.run ? crashed.run.task_id : lastRun ? lastRun.task_id : null;
  // The queue scrolls on its own, opened at its end ("next"); again whenever the running task or the toggle changes.
  const qbox = useRef(null), H = useHeight(qbox), atEnd = useRef(null);
  useLayoutEffect(() => {
    const key = `${currents.map((x) => x.t.id).join(",")} ${showBlocked}`;
    if (!H || atEnd.current === key) return;
    atEnd.current = key;
    qbox.current.scrollTop = qbox.current.scrollHeight;
  }, [H, currents.map((x) => x.t.id).join(","), showBlocked]);
  const tl = useRef(null);
  useMoves(tl);
  const selTask = sel != null ? d.task.get(sel) : null;
  return html`<div class="live">
    <div class="tl" ref=${tl}>
      <div class="tl-head"><span class="tl-label">queue, next nearest${showBlocked && blocked.length ? "; blocked where they would queue" : ""}</span>
        <label class="tl-toggle"><input type="checkbox" checked=${showBlocked} onChange=${(e) => toggleBlocked(e.target.checked)} />
          blocked${blocked.length ? html` <span class="muted num">(${blocked.length})</span>` : null}</label></div>
      <div class="tl-pane" ref=${qbox}><div class="tl-end">${queue.length ? null : html`<div class="tl-label">The queue is empty.</div>`}
        ${queue.map((t) => {
          const q = qpos.get(t.id);
          if (q == null) return html`<${Item} key=${t.id} id=${t.id} sel=${sel} select=${setPicked} cls="blocked" zone="queue">
            <span class="tl-pos num" title="waits for a pending decision or a prerequisite task">—</span>
            <span class="tl-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} /> <${Worktree} name=${t.worktree} />
              <span class=${`tag waits${d.stuck(t) ? " stuck" : ""}`}>waits for ${[...d.blockers(t).map((x) => `d${x.id}`), ...d.unmet(t).map((x) => `t${x.id}${x.status === "dropped" ? " (dropped)" : ""}`)].join(", ")}</span></span><${Prio} p=${d.prio(t)} /></${Item}>`;
          return html`<${Item} key=${t.id} id=${t.id} sel=${sel} select=${setPicked} zone="queue">
            <span class="tl-pos num">${q === 1 ? "next" : `#${q}`}</span>
            <span class="tl-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} /> <${Worktree} name=${t.worktree} />${failedLast(t) ? html` <${Pill} status="failed" />` : null}${d.waitsFor(t) ? html` <span class="tag waits" title=${d.waitsFor(t)}>${d.holds.has(t.id) ? `waits for t${d.holds.get(t.id)}` : "waits: uncommitted"}</span>` : null}</span><${Prio} p=${d.prio(t)} /></${Item}>`;
        })}</div></div>
      <div class="tl-current">
        ${now.length ? now.map(({ r, t, wrapping }) => wrapping ? html`<div class="tl-idle tl-busy"><span class="spinner"></span><div>
            <div class="sub">${wrapText(t).text} <${Worktree} name=${r.worktree} /></div><div class="muted">${wrapText(t).sub}</div></div></div>`
          : html`<${Item} key=${t.id} id=${t.id} sel=${sel} select=${setPicked} cls="now" zone="now">
            <div class="grow"><div class="row"><${Pill} status="running" /><span class="sub">task ${t.id} · <${Elapsed} start=${r.started_at} /></span><${Worktree} name=${r.worktree} /></div>
            <div class="tl-current-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} /></div>
            <div class="muted">${t.plan_id != null ? html`<${Named} kind="plan" r=${d.plan.get(t.plan_id)} /> · <${Named} kind="milestone" r=${d.milestoneOfTask(t)} />` : "executive intervention"}</div>
            <${Handoff} d=${d} r=${r} /></div></${Item}>`)
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
      <div class="tl-pane">${retired.length ? html`<div class="tl-label">done and dropped, newest first</div>` : null}
        ${retired.map((t) => {
          const runs = d.taskRuns(t);
          return html`<${Item} key=${t.id} id=${t.id} sel=${sel} select=${setPicked} cls="retired" zone="retired"><${Pill} status=${t.status} />
            <span class="tl-subject"><${Named} kind="task" r=${t} /> <${Kind} kind=${t.kind} /> <${Worktree} name=${t.worktree} /></span>
            <span class="muted num" style="white-space:nowrap">${runs.length ? usd(d.cost(runs)) + " · " : ""}<${Time} ts=${t.retired_at} /></span></${Item}>`;
        })}</div>
    </div>
    <div class="live-out">${selTask ? html`<${Task} key=${selTask.id} d=${d} id=${selTask.id} embedded />`
      : html`<div class="empty">No run yet. Select a task on the left.</div>`}</div>
  </div>`;
}
