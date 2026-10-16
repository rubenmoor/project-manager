// Adding milestones, plans and tasks; the welcome screen of a project without milestones.
import { html, useState, useRef } from "/vendor/preact-htm.js";
import { CodeEditor, post, autofocus } from "/editor.js";
import { NamedLink } from "/ui.js";

const WHAT = {
  milestone: { title: "New milestone", ph: "Users can export their data as CSV", body: "", bodyPh: "A description, and a few constraints as bullet points",
    hint: "The milestone is the only text a worker gets from you. pm adds the masterplan and the task that writes it." },
  task: { title: "New task", ph: "Add the export endpoint", body: "", hint: "" },
  exec: { title: "New executive intervention", ph: "Check the CI config before the next release", body: "",
    hint: "A task of yours outside every plan and milestone: queued as if at milestone queue index 0, by priority." },
};

export function AddForm({ kind, parent, onClose }) {
  const w = WHAT[kind];
  const [subject, setSubject] = useState("");
  const [prio, setPrio] = useState("");
  const [queue, setQueue] = useState("");
  const [taskKind, setTaskKind] = useState("work");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const body = useRef(w.body);
  const submit = async () => {
    if (busy) return;
    const op = { op: `${kind === "exec" ? "task" : kind}_add`, subject, body: body.current };
    if (kind === "task") { op.plan = parent; op.kind = taskKind; }
    if (kind === "exec") op.kind = "exec";
    if (kind === "milestone" && queue.trim() !== "") {
      if (!/^-?\d+$/.test(queue.trim())) { setError("the queue index is a whole number, or empty"); return; }
      op.queue_index = Number(queue);
    }
    if (kind !== "milestone" && prio.trim() !== "") {
      if (!/^-?\d+$/.test(prio.trim())) { setError("the priority is a whole number, or empty"); return; }
      op.priority = Number(prio);
    }
    setBusy(true);
    try {
      const r = await post(op);
      onClose && onClose();
      location.hash = kind === "milestone" ? `#/added/${r.id}` : `#/${kind === "exec" ? "task" : kind}/${r.id}`;
    } catch (e) { setError(e.message); setBusy(false); }
  };
  const keys = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
    if (e.key === "Escape" && onClose && !e.target.closest(".cm-editor")) onClose();
  };
  return html`<div class="card addform" onKeyDown=${keys}>
    <div class="section-head"><b>${w.title}</b>${onClose ? html`<button class="iconbtn" onClick=${onClose}>cancel</button>` : null}</div>
    ${w.hint ? html`<div class="sub" style="margin:4px 0 10px">${w.hint}</div>` : null}
    <input class="line-edit h1" placeholder=${w.ph} value=${subject} onInput=${(e) => setSubject(e.target.value)} ref=${autofocus} />
    <div class="cm-frame"><${CodeEditor} value=${w.body} autofocus=${false} onChange=${(v) => (body.current = v)} ph=${w.bodyPh || "Body (Markdown)"} /></div>
    <div class="row" style="margin-top:10px">
      ${kind === "milestone" ? html`<label class="sub" title="tasks of milestones with a lower queue index are worked first">queue index
        <input class="small" value=${queue} placeholder="0" onInput=${(e) => setQueue(e.target.value)} /></label>` : null}
      ${kind !== "milestone" ? html`<label class="sub">priority <input class="small" value=${prio} placeholder=${kind === "task" ? "plan's" : kind === "exec" ? "0" : "none"}
        onInput=${(e) => setPrio(e.target.value)} /></label>` : null}
      ${kind === "task" ? html`<label class="sub">kind <select class="small" value=${taskKind} onChange=${(e) => setTaskKind(e.target.value)}>
        <option value="work">work</option><option value="plan">plan (writes a plan)</option></select></label>` : null}
      <span class="grow"></span>
      ${error ? html`<span class="err">${error}</span>` : null}
      <button class="btn primary" onClick=${submit} disabled=${busy}>Add ${kind === "exec" ? "exec task" : kind}</button>
    </div>
    <div class="edit-hint">Markdown supported · Ctrl-Enter: add</div>
  </div>`;
}

// After a milestone is added: add another one, or start the workers.
export function MilestoneAdded({ d, id }) {
  const [more, setMore] = useState(false);
  const [copied, setCopied] = useState(false);
  const m = d.milestone.get(id);
  if (more) return html`<div class="welcome"><div class="welcome-form"><${AddForm} kind="milestone" onClose=${() => setMore(false)} /></div></div>`;
  const copy = () => navigator.clipboard.writeText("pm run").then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => {});
  return html`<div class="welcome"><div class="welcome-inner added">
    <div class="welcome-mark">✓</div>
    <h1>Milestone added</h1>
    <p class="sub">${m ? html`<${NamedLink} kind="milestone" r=${m} />` : `milestone ${id}`}. pm queued the task that writes its masterplan;
      workers take it from there: plans, tasks, the work.</p>
    ${d.running ? html`<div class="card next running-now"><b>pm run is working.</b>
        <div class="sub">It picks up the new milestone's tasks by priority. Follow it on the <a href="#/live">live view</a>.</div></div>`
      : html`<div class="card next"><b>Next: start the workers in the project's terminal</b>
        <div class="cmd"><code>pm run</code><button class="iconbtn" onClick=${copy}>${copied ? "copied" : "copy"}</button></div>
        <div class="sub"><code>pm run</code> isn't running. Nothing happens until you start it; the dashboard follows it live.</div></div>`}
    <div class="row" style="justify-content:center;margin-top:20px">
      <button class="btn primary" onClick=${() => setMore(true)}>Add another milestone</button></div>
  </div></div>`;
}

export function AddButton({ kind, parent, label }) {
  const [open, setOpen] = useState(false);
  if (open) return html`<${AddForm} kind=${kind} parent=${parent} onClose=${() => setOpen(false)} />`;
  return html`<button class="btn" onClick=${() => setOpen(true)}>+ ${label || `Add ${kind}`}</button>`;
}

export function Welcome() {
  const [open, setOpen] = useState(false);
  return html`<div class="welcome">
    ${open ? html`<div class="welcome-form"><${AddForm} kind="milestone" onClose=${() => setOpen(false)} /></div>`
      : html`<div class="welcome-inner">
        <div class="welcome-mark">pm</div>
        <h1>Nothing planned yet</h1>
        <p class="sub">Describe what this project should achieve next. Workers turn it into a masterplan, plans and tasks;
          <code>pm run</code> works them one by one.</p>
        <button class="btn primary big" onClick=${() => setOpen(true)}>Add the first milestone</button>
      </div>`}
  </div>`;
}
