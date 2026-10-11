// Adding milestones, plans and tasks; the welcome screen of a project without milestones.
import { html, useState, useRef } from "/vendor/preact-htm.js";
import { CodeEditor, post, autofocus } from "/editor.js";

const MILESTONE_TEMPLATE = `What should be true afterwards, and why.

## Outcome
-

## Constraints
-

## Out of scope
-

## Done when
- `;

const WHAT = {
  milestone: { title: "New milestone", ph: "Users can export their data as CSV", body: MILESTONE_TEMPLATE,
    hint: "The milestone is the only text a worker gets from you: what should be true afterwards, why, what is fixed, what is out of scope, how to tell it's done. pm adds the masterplan and the task that writes it." },
  plan: { title: "New plan", ph: "Export API endpoint", body: "", hint: "pm queues a task that splits the plan into tasks." },
  task: { title: "New task", ph: "Add the export endpoint", body: "", hint: "" },
};

export function AddForm({ kind, parent, onClose }) {
  const w = WHAT[kind];
  const [subject, setSubject] = useState("");
  const [prio, setPrio] = useState("");
  const [taskKind, setTaskKind] = useState("work");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const body = useRef(w.body);
  const submit = async () => {
    if (busy) return;
    const op = { op: `${kind}_add`, subject, body: body.current };
    if (kind === "plan") op.milestone = parent;
    if (kind === "task") { op.plan = parent; op.kind = taskKind; }
    if (kind !== "milestone" && prio.trim() !== "") {
      if (!/^-?\d+$/.test(prio.trim())) { setError("the priority is a whole number, or empty"); return; }
      op.priority = Number(prio);
    }
    setBusy(true);
    try {
      const r = await post(op);
      onClose && onClose();
      location.hash = `#/${kind}/${r.id}`;
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
    <div class="cm-frame"><${CodeEditor} value=${w.body} autofocus=${false} onChange=${(v) => (body.current = v)} ph="Body (Markdown)" /></div>
    <div class="row" style="margin-top:10px">
      ${kind !== "milestone" ? html`<label class="sub">priority <input class="small" value=${prio} placeholder=${kind === "task" ? "plan's" : "none"}
        onInput=${(e) => setPrio(e.target.value)} /></label>` : null}
      ${kind === "task" ? html`<label class="sub">kind <select class="small" value=${taskKind} onChange=${(e) => setTaskKind(e.target.value)}>
        <option value="work">work</option><option value="plan">plan (writes a plan)</option></select></label>` : null}
      <span class="grow"></span>
      ${error ? html`<span class="err">${error}</span>` : null}
      <button class="btn primary" onClick=${submit} disabled=${busy}>Add ${kind}</button>
    </div>
    <div class="edit-hint">Ctrl-Enter: add</div>
  </div>`;
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
