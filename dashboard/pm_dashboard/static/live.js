// The run view: left, one timeline (queue above, highest priority nearest; the running task; retired tasks below,
// newest first); right, the running task's output.
import { html, useState, useEffect, useRef, useLayoutEffect } from "/vendor/preact-htm.js";
import { Pill, Kind, Prio, Elapsed, Time } from "/ui.js";
import { Log } from "/views.js";
import { usd } from "/data.js";

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

function QueueItem({ d, t, pos }) {
  return html`<a class="tl-item" href=${`#/task/${t.id}`}>
    <span class="tl-pos num">${pos === 1 ? "next" : `#${pos}`}</span>
    <span class="tl-subject">${t.subject} <${Kind} kind=${t.kind} /></span>
    <${Prio} p=${d.prio(t)} /></a>`;
}

function RetiredItem({ d, t }) {
  const runs = d.taskRuns(t);
  return html`<a class="tl-item retired" href=${`#/task/${t.id}`}>
    <${Pill} status=${t.status} />
    <span class="tl-subject">${t.subject} <${Kind} kind=${t.kind} /></span>
    <span class="muted num" style="white-space:nowrap">${runs.length ? usd(d.cost(runs)) + " · " : ""}<${Time} ts=${t.retired_at} /></span></a>`;
}

export function Live({ d }) {
  const r = d.running;
  const last = r || [...d.runs].filter((x) => x.ended_at).sort((a, b) => (a.ended_at < b.ended_at ? 1 : -1))[0];
  const current = r ? d.task.get(r.task_id) : null;
  const queue = d.queue.filter((id) => !current || id !== current.id).map((id) => d.task.get(id)).reverse();
  const retired = d.tasks.filter((t) => t.retired_at).sort((a, b) => (a.retired_at < b.retired_at ? 1 : a.retired_at > b.retired_at ? -1 : b.id - a.id));
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
  const logTask = last ? d.task.get(last.task_id) : null;
  return html`<div class="live">
    <div class="tl" ref=${scroller}>
      <div style=${`height:${padTop}px`}></div>
      <div ref=${top}>${queue.length ? html`<div class="tl-label">queue, next nearest</div>` : null}
        ${queue.map((t, i) => html`<${QueueItem} d=${d} t=${t} pos=${queue.length - i} />`)}</div>
      <div class="tl-current" ref=${card}>
        ${current ? html`<div class="row"><${Pill} status="running" /><span class="sub">task ${current.id} · <${Elapsed} start=${r.started_at} /></span></div>
          <div class="tl-current-subject"><a href=${`#/task/${current.id}`}>${current.subject}</a> <${Kind} kind=${current.kind} /></div>
          <div class="muted">${d.plan.get(current.plan_id).subject} · ${d.milestoneOfTask(current).subject}</div>`
          : html`<div class="sub">No worker is running.</div>
            <div class="muted">${d.queue.length ? html`Next: <a href=${`#/task/${d.queue[0]}`}>${d.task.get(d.queue[0]).subject}</a>` : "The queue is empty."}</div>`}
      </div>
      <div ref=${bottom}>${retired.length ? html`<div class="tl-label">done and dropped, newest first</div>` : null}
        ${retired.map((t) => html`<${RetiredItem} d=${d} t=${t} />`)}</div>
      <div style=${`height:${padBottom}px`}></div>
    </div>
    <div class="live-out">
      <div class="live-head">${logTask ? html`<b>${r ? "Output" : "Last output"}</b> <a href=${`#/task/${logTask.id}`}>task ${logTask.id}: ${logTask.subject}</a>
        ${!r ? html` <${Pill} status=${last.result} />` : null}` : html`<b>Output</b>`}</div>
      ${logTask ? html`<${Log} d=${d} t=${logTask} live=${!!r} full />` : html`<div class="empty">No run yet.</div>`}
    </div>
  </div>`;
}
