// Resolving a pending decision on its page, as `pm decision answer|approve|return|reject|irrelevant`: the answer, feedback or reason is a
// Markdown draft (rendered; a click: source; Esc: rendered again), sent with a button or Ctrl-Enter; "& next"
// (Ctrl-Shift-Enter) then opens the next pending decision.
import { html, useState, useRef, useEffect } from "/vendor/preact-htm.js";
import { CodeEditor, post, useEditTrigger, loadDraft, saveDraft, useKept } from "/editor.js";
import { Markdown, InlineMd } from "/ui.js";

function Draft({ value, set, empty }) {
  const [editing, setEditing] = useState(false);
  const t = useEditTrigger(() => setEditing(true), false);
  const was = useRef(false);
  useEffect(() => { if (was.current && !editing) t.ref.current.focus(); was.current = editing; }, [editing]);
  if (editing) return html`<div class="editing"><${CodeEditor} value=${value} onChange=${set} ph="Markdown"
    onDone=${(v) => { set(v); setEditing(false); }} /></div>`;
  return html`<div class="card editable" tabindex="0" ref=${t.ref} onKeyDown=${t.onKeyDown} onClick=${t.onClick} title="i: write">
    <${Markdown} text=${value} empty=${empty} /></div>`;
}

function useDraft(key) {
  const [value, setValue] = useState(() => loadDraft(key) || "");
  const cur = useRef(value);
  const set = (v) => { cur.current = v; setValue(v); saveDraft(key, v); };
  return { value, set, cur };
}

// Reject a sign-off (or one task of a task-queue sign-off): an optional one-line reason; Enter or Submit, Esc cancels.
function Reject({ draft, what, busy, error, submit, cancel }) {
  const [reason, setReason] = useKept(draft);
  const input = useRef(null);
  useEffect(() => input.current && input.current.focus(), []);
  const key = (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(reason); }
    else if (e.key === "Escape") { e.preventDefault(); cancel(); }
  };
  return html`<div class="row reject">
    <span class="sub">Reject: archives ${what}. Kept, but hidden.</span>
    <input class="note-input grow" ref=${input} value=${reason} placeholder="reason (optional)"
      onInput=${(e) => setReason(e.target.value)} onKeyDown=${key} />
    ${error ? html`<span class="err">${error}</span>` : null}
    <button class="iconbtn" onClick=${cancel}>cancel</button>
    <button class="btn danger" onClick=${() => submit(reason)} disabled=${busy}>Submit</button></div>`;
}

const REJECTS = { masterplan: "the masterplan, the plans below it and their open tasks; the milestone goes on hold",
  plan: "the plan, the plans below it and their open tasks", "task-queue": "every task waiting for it", web: "the task" };

export function Resolve({ d, x, next, view = "" }) {
  const [mode, setMode] = useState("answer");
  const [rejecting, setRejecting] = useState(null);   // "all", or a task id
  const answer = useDraft(`decision:${x.id}:answer`);
  const reason = useDraft(`decision:${x.id}:reason`);
  const feedback = useDraft(`decision:${x.id}:feedback`);
  const returnable = x.signoff_plan != null;
  const [once, setOnce] = useState(x.kind === "once");
  const [sup, setSup] = useKept(`decision:${x.id}:supersedes`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const open = (d.tasksWaiting.get(x.id) || []).filter((t) => t.status === "open").length;
  // then: after success, open the next pending decision, or the Decisions list when none is left.
  const go = async (op, draft, then) => {
    if (busy) return;
    setBusy(true);
    try {
      await post(op); draft && draft.set(""); setError(null); setBusy(false);
      if (then) location.hash = next != null ? `#/decision/${next}${view}` : `#/decisions${view}`;
      return true;
    }
    catch (e) { setError(e.message); setBusy(false); return false; }
  };
  const submit = (then) => {
    if (mode === "return") return go({ op: "decision_return", id: x.id, feedback: feedback.cur.current }, feedback, then);
    if (x.signoff) return go({ op: "decision_approve", id: x.id }, null, then);
    if (mode === "irrelevant") return go({ op: "decision_irrelevant", id: x.id, reason: reason.cur.current }, reason, then);
    const ids = (sup.match(/\d+/g) || []).map(Number);
    go({ op: "decision_answer", id: x.id, answer: answer.cur.current, once, supersedes: ids }, answer, then).then((ok) => ok && setSup(""));
  };
  const nextBtn = (label, off) => html`<button class="btn primary" onClick=${() => submit(true)} disabled=${off}
    title=${next != null ? `then open decision ${next}` : "none left: then back to the decisions"}>${label} & next</button>`;
  const keys = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(e.shiftKey); } };
  const status = html`<span class="grow"></span>${error ? html`<span class="err">${error}</span>` : null}`;
  if (mode === "return") return html`<div class="resolve" onKeyDown=${keys}>
    <h2>Return with feedback</h2>
    <${Draft} value=${feedback.value} set=${feedback.set} empty="What should change: click or press i to write." />
    <div class="row" style="margin-top:10px">
      <span class="sub">A planning task revises the plan with your feedback; then it comes back for sign-off.</span>
      ${status}<button class="iconbtn" onClick=${() => { setMode("answer"); setError(null); }}>cancel</button>
      <button class="btn" onClick=${() => submit()} disabled=${busy || !feedback.value.trim()}>Return</button>
      ${nextBtn("Return", busy || !feedback.value.trim())}</div>
    <div class="edit-hint">Markdown supported · Esc: render · Ctrl-Enter: return · Ctrl-Shift-Enter: return & next</div></div>`;
  const reject = async (reason) => {
    if (await go({ op: "decision_reject", id: x.id, reason, ...(rejecting === "all" ? {} : { task: rejecting }) })) {
      saveDraft(`decision:${x.id}:reject:${rejecting}`, null); setRejecting(null);
    }
  };
  const openTasks = (d.tasksWaiting.get(x.id) || []).filter((t) => t.status === "open");
  const cancelReject = () => { setRejecting(null); setError(null); };
  if (x.signoff) return html`<div class="card resolve">
    ${rejecting === "all" ? html`<${Reject} draft=${`decision:${x.id}:reject:all`} what=${REJECTS[x.signoff]} busy=${busy} error=${error} submit=${reject} cancel=${cancelReject} />`
    : html`<div class="row"><span class="sub">${open ? `Unblocks ${open} open task${open > 1 ? "s" : ""}. ` : ""}${returnable
      ? "Edit the plan above, or return it with feedback." : "Leave it pending to change plans or tasks first."}</span>
      ${rejecting == null ? status : null}${returnable ? html`<button class="iconbtn" onClick=${() => { setMode("return"); setError(null); }}>return with feedback…</button>` : null}
      <button class="btn danger" onClick=${() => { setRejecting("all"); setError(null); }} disabled=${busy}>Reject</button>
      <button class="btn" onClick=${() => submit()} disabled=${busy}>Approve</button>${nextBtn("Approve", busy)}</div>`}
    ${x.signoff === "task-queue" && openTasks.length > 1 ? html`<div class="reject-one"><div class="sub">Or reject one task; the sign-off stays pending for the others:</div>
      <ul class="plain">${openTasks.map((t) => html`<li>${rejecting === t.id
        ? html`<a href=${`#/task/${t.id}`}>task ${t.id}</a> ${t.subject}<${Reject} draft=${`decision:${x.id}:reject:${t.id}`} what=${`task ${t.id}`} busy=${busy} error=${error} submit=${reject} cancel=${cancelReject} />`
        : html`<a href=${`#/task/${t.id}`}>task ${t.id}</a> ${t.subject} <button class="iconbtn" disabled=${busy}
          onClick=${() => { setRejecting(t.id); setError(null); }}>reject…</button>`}</li>`)}</ul></div>` : null}</div>`;
  if (mode === "irrelevant") return html`<div class="resolve" onKeyDown=${keys}>
    <h2>Close as irrelevant</h2>
    <${Draft} value=${reason.value} set=${reason.set} empty="Why it no longer matters: click or press i to write." />
    <div class="row" style="margin-top:10px">
      <span class="sub">${open ? `Drops the ${open} open task${open > 1 ? "s" : ""} waiting for it.` : "No open task waits for it."}</span>
      ${status}<button class="iconbtn" onClick=${() => { setMode("answer"); setError(null); }}>cancel</button>
      <button class="btn danger" onClick=${() => submit()} disabled=${busy || !reason.value.trim()}>Close as irrelevant</button></div>
    <div class="edit-hint">Markdown supported · Esc: render · Ctrl-Enter: close · Ctrl-Shift-Enter: close & next</div></div>`;
  return html`<div class="resolve" onKeyDown=${keys}>
    <h2>Your answer</h2>
    ${x.options.length ? html`<ol class="options pick">${x.options.map((o, i) => html`<li class=${i === 0 ? "rec" : ""}>
      <button class=${"option" + (answer.value === o ? " chosen" : "")} title="use this option as the answer"
        onClick=${() => answer.set(o)}><${InlineMd} text=${o} /></button></li>`)}</ol>` : null}
    <${Draft} value=${answer.value} set=${answer.set}
      empty=${x.options.length ? "Pick an option, or click or press i to write your own answer." : "Click or press i to write your answer."} />
    <div class="row" style="margin-top:10px">
      ${x.kind === "once" ? html`<span class="tag">once: not in the log</span>`
        : html`<label class="sub" title="a one-off go-ahead or check, not a lasting rule"><input type="checkbox" checked=${once}
          onChange=${(e) => setOnce(e.target.checked)} /> one-off, not in the decision log</label>`}
      <label class="sub" title="answered decisions this answer replaces">supersedes <input class="small" value=${sup}
        placeholder="ids" onInput=${(e) => setSup(e.target.value)} /></label>
      ${status}<button class="iconbtn" onClick=${() => { setMode("irrelevant"); setError(null); }}>irrelevant…</button>
      <button class="btn" onClick=${() => submit()} disabled=${busy || !answer.value.trim()}>Answer</button>
      ${nextBtn("Answer", busy || !answer.value.trim())}</div>
    <div class="edit-hint">Markdown supported · Esc: render · Ctrl-Enter: answer · Ctrl-Shift-Enter: answer & next${open ? ` · unblocks ${open} open task${open > 1 ? "s" : ""}` : ""}</div></div>`;
}
