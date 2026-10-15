// Every task: the decisions blocking the most on top, then the exec tasks (no plan), then by milestone and plan.
import { html, useState } from "/vendor/preact-htm.js";
import { Named, NamedLink, TaskLink, Kind, Pill } from "/ui.js";
import { AddButton } from "/add.js";
import { TaskRows, Chips, counts } from "/views.js";

const n = (k, word) => `${k} ${word}${k === 1 ? "" : "s"}`;

function Blocking({ d }) {
  const bs = d.blocking();
  if (!bs.length) return html`<div class="empty">No task waits for a decision.</div>`;
  const [top] = bs;
  return html`${top.alone.length ? html`<div class="banner warn">Answer <a href=${`#/decision/${top.x.id}`}>decision ${top.x.id}: ${top.x.subject}</a>${" "}
      to unblock ${n(top.alone.length, "task")} at once.</div>` : null}
    ${bs.map((b) => html`<div class="plan">
      <div class="plan-head"><a href=${`#/decision/${b.x.id}`}><b><${Named} kind="decision" r=${b.x} /></b></a>
        ${b.x.signoff ? html`<span class="tag signoff">${b.x.signoff}</span>` : null}
        <span class="muted">unblocks ${b.alone.length} · ${b.waiting.length} waiting</span></div>
      <div class="tasks"><table><tbody>${b.waiting.map((t) => {
        const m = d.milestoneOfTask(t);
        const others = d.blockers(t).filter((x) => x.id !== b.x.id);
        return html`<tr><td class="subject"><${TaskLink} t=${t} /> <${Kind} kind=${t.kind} /></td>
          <td class="muted">${m ? html`<${Named} kind="milestone" r=${m} />` : "executive intervention"}</td>
          <td class="muted" style="width:200px">${others.length ? `also waits for ${others.map((x) => `d${x.id}`).join(", ")}` : "only this one"}</td></tr>`;
      })}</tbody></table></div></div>`)}`;
}

export function Tasks({ d }) {
  const [show, setShow] = useState("open");
  const filt = (ts) => ts.filter((t) => show === "all" || (show === "open" ? t.status === "open" : t.status !== "open"));
  const qpos = (t) => d.queuePos.get(t.id) ?? Infinity;
  const execs = filt(d.tasks.filter((t) => t.kind === "exec"))
    .sort((a, b) => (a.status !== "open") - (b.status !== "open") || qpos(a) - qpos(b) || b.id - a.id);
  const ms = [...d.milestones].sort((a, b) => a.queue_index - b.queue_index || a.id - b.id);
  return html`<h1>Tasks</h1>
    <div class="sub">Every task. On top, the pending decisions that hold tasks back, the one that frees the most first.</div>
    <${Chips} value=${show} set=${setShow} options=${[["open", "open tasks"], ["closed", "closed"], ["all", "all"]]} />
    <h2>Blocked by decisions</h2>
    <${Blocking} d=${d} />
    <h2>Executive interventions</h2>
    <div class="sub">Tasks of yours outside every plan and milestone, queued as if at milestone queue index 0, by priority.</div>
    <div class="plan"><div class="tasks" style="border-top:1px solid var(--border);border-radius:8px">
      ${execs.length ? html`<${TaskRows} d=${d} tasks=${execs} />` : html`<div class="empty" style="padding:10px 12px">None.</div>`}</div></div>
    <${AddButton} kind="exec" label="Add an executive intervention" />
    <h2>By milestone</h2>
    ${ms.map((m) => {
      const ts = filt(d.milestoneTasks(m));
      if (!ts.length) return null;
      return html`<h3><${NamedLink} kind="milestone" r=${m} /> ${m.status !== "open" ? html`<${Pill} status=${m.status} />` : null}
        <span class="muted">queue index ${m.queue_index}</span></h3>
        ${d.plansSorted(m).map((p) => {
          const pts = filt(d.tasksOf.get(p.id) || []);
          if (!pts.length) return null;
          const c = counts(d.tasksOf.get(p.id) || []);
          return html`<details class="plan" open=${!d.planDone.has(p.id)}>
            <summary><a href=${`#/plan/${p.id}`} onClick=${(e) => e.stopPropagation()}><b><${Named} kind="plan" r=${p} /></b></a>
              <span class="muted">${c.open} open · ${c.done} done${c.dropped ? ` · ${c.dropped} dropped` : ""}</span></summary>
            <div class="tasks"><${TaskRows} d=${d} tasks=${pts} /></div></details>`;
        })}`;
    })}`;
}
