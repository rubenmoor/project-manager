// Edit in place: a text shows rendered; Enter or a click shows its source (CodeMirror, monospace);
// Esc or leaving it saves and renders it again.
import { html, useState, useEffect, useRef } from "/vendor/preact-htm.js";
import {
  EditorView, EditorState, keymap, placeholder, drawSelection, history, historyKeymap, defaultKeymap, indentWithTab,
  markdown, markdownLanguage, syntaxHighlighting, HighlightStyle, tags, closeBrackets, closeBracketsKeymap,
  searchKeymap, highlightSelectionMatches,
} from "/vendor/codemirror.js";
import { Markdown } from "/ui.js";
import { PROJECT } from "/projects.js";

// Unsubmitted input: kept in localStorage per project until submitted or emptied; dropped after 30 days.
const DRAFT = `pm-draft:${PROJECT ?? location.pathname}:`;
export function loadDraft(k) { try { const s = localStorage.getItem(DRAFT + k); return s ? JSON.parse(s).v : null; } catch { return null; } }
export function saveDraft(k, v) {
  try { v == null || v === "" ? localStorage.removeItem(DRAFT + k) : localStorage.setItem(DRAFT + k, JSON.stringify({ v, t: Date.now() })); } catch {}
}
// Upload placeholders in drafts: replaced when the upload ends, wherever the text is now; left over from a closed page: removed.
const MARK = /!\[uploading image [^\]]*…\]\(\)\n?/g;
function swapDrafts(f) {
  try {
    for (const k of Object.keys(localStorage)) {
      if (!k.startsWith("pm-draft:")) continue;
      const s = localStorage.getItem(k), d = JSON.parse(s);
      const v = JSON.stringify(d.v, (_, x) => typeof x === "string" ? f(x) : x);
      if (!(d.t > Date.now() - 30 * 864e5)) localStorage.removeItem(k);
      else if (v !== JSON.stringify(d.v)) localStorage.setItem(k, JSON.stringify({ v: JSON.parse(v), t: d.t }));
    }
  } catch {}
}
swapDrafts((x) => x.replace(MARK, ""));

// A string state kept as a draft under `k`.
export function useKept(k, init = "") {
  const [v, setV] = useState(() => loadDraft(k) ?? init);
  return [v, (x) => { setV(x); saveDraft(k, x === init ? null : x); }];
}

// A ref that focuses and selects the element as soon as it is mounted.
export const autofocus = (el) => {
  if (el && !el.dataset.focused) { el.dataset.focused = "1"; setTimeout(() => { el.focus(); el.select && el.select(); }); }
};

export async function post(op) {
  const r = await fetch("api/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op) });
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

// Pasted or dropped images: uploaded (pm stores and commits them in pm-images/), a Markdown image at the cursor.
async function upload(file) {
  const r = await fetch("api/image", { method: "POST", headers: { "Content-Type": file.type }, body: file });
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j.path;
}

let uploads = 0;
const views = new Set();
function insertImages(view, files, pos, fail) {
  for (const file of files) {
    const mark = `![uploading image ${Date.now().toString(36)}-${++uploads}…]()`;
    const before = view.state.doc.sliceString(Math.max(0, pos - 2), pos);
    const text = (pos === 0 || before.endsWith("\n\n") || before === "\n" ? "" : before.endsWith("\n") ? "\n" : "\n\n") + mark + "\n";
    view.dispatch({ changes: { from: pos, insert: text }, selection: { anchor: pos + text.length } });
    pos += text.length;
    const swap = (by) => {
      for (const v of views) {
        const at = v.state.doc.toString().indexOf(mark);
        if (at >= 0) v.dispatch({ changes: { from: at, to: at + mark.length + (by ? 0 : 1), insert: by } });
      }
      swapDrafts((x) => x.split(by ? mark : mark + "\n").join(by).split(mark).join(by));
    };
    upload(file).then((path) => { swap(`![](${path})`); fail(null); },
      (e) => { swap(""); fail(`image not added: ${e.message}`); });
  }
}
const images = (list) => [...(list || [])].filter((f) => f.type.startsWith("image/") && f.size > 0);

const theme = EditorView.theme({
  "&": { background: "var(--surface)", color: "var(--ink)", fontSize: "13px", borderRadius: "8px" },
  "&.cm-focused": { outline: "none" },
  ".cm-content": { fontFamily: "var(--mono)", caretColor: "var(--ink)", padding: "12px 0" },
  ".cm-line": { padding: "0 14px" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--ink)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": { background: "var(--accent-wash) !important" },
  ".cm-placeholder": { color: "var(--muted)" },
  ".cm-selectionMatch": { background: "var(--warning-wash)" },
});

const highlight = HighlightStyle.define([
  { tag: tags.heading, fontWeight: "700", color: "var(--ink)" },
  { tag: tags.strong, fontWeight: "700" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: tags.strikethrough, textDecoration: "line-through" },
  { tag: [tags.link, tags.url], color: "var(--accent)" },
  { tag: tags.monospace, color: "var(--good-text)" },
  { tag: tags.quote, color: "var(--ink-2)" },
  { tag: [tags.processingInstruction, tags.meta, tags.contentSeparator], color: "var(--muted)" },
  { tag: tags.list, color: "var(--ink)" },
]);

export function CodeEditor({ value, onDone, onChange, autofocus = true, ph = "" }) {
  const box = useRef(null);
  const done = useRef(onDone);
  done.current = onDone;
  const change = useRef(onChange);
  change.current = onChange;
  const [error, setError] = useState(null);
  useEffect(() => {
    const finish = (v) => { done.current && done.current(v.state.doc.toString()); return true; };
    const view = new EditorView({
      parent: box.current,
      state: EditorState.create({
        doc: value || "",
        extensions: [
          history(), drawSelection(), closeBrackets(), highlightSelectionMatches(), EditorView.lineWrapping,
          markdown({ base: markdownLanguage }), syntaxHighlighting(highlight), theme, placeholder(ph),
          keymap.of([{ key: "Escape", run: finish }, { key: "Mod-Enter", run: finish },
            ...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
          EditorView.domEventHandlers({
            blur: (e, v) => { if (!box.current.contains(e.relatedTarget)) finish(v); },
            paste: (e, v) => {
              const files = images(e.clipboardData && e.clipboardData.files).slice(0, 1);   // one image, maybe in several formats
              if (!files.length) return false;
              e.preventDefault();
              insertImages(v, files, v.state.selection.main.head, setError);
              return true;
            },
            drop: (e, v) => {
              const files = images(e.dataTransfer && e.dataTransfer.files);
              if (!files.length) return false;
              e.preventDefault();
              insertImages(v, files, v.posAtCoords({ x: e.clientX, y: e.clientY }) ?? v.state.selection.main.head, setError);
              return true;
            },
          }),
          EditorView.updateListener.of((u) => {
            if (u.docChanged && change.current) try { change.current(u.state.doc.toString()); } catch (e) { console.error(e); }
          }),
        ],
      }),
    });
    views.add(view);
    if (autofocus) { view.focus(); view.dispatch({ selection: { anchor: view.state.doc.length } }); }
    return () => { views.delete(view); view.destroy(); };
  }, []);
  return html`<div class="cm-wrap"><span class="md-badge">Markdown</span><div class="cm-box" ref=${box}></div>
    ${error ? html`<span class="err">${error}</span>` : null}</div>`;
}

// Starts editing on Enter on the focused element, or a click (not on a link).
export function useEditTrigger(start, disabled) {
  const ref = useRef(null);
  const onKeyDown = (e) => {
    if (!disabled && e.key === "Enter" && e.target === e.currentTarget && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); start(); }
  };
  const onClick = (e) => { if (!disabled && !e.target.closest("a") && !getSelection().toString()) start(); };
  return { ref, onKeyDown, onClick };
}

// Saves with the text editing started from (`base`): the server refuses the save if the row changed since.
// An unsaved edit is kept as a draft under `dk` and reopens in the editor.
function useSave(text, save, dk) {
  let init = useRef(dk && save ? loadDraft(dk) : null).current;
  if (init && init.v === text) { saveDraft(dk, null); init = null; }
  const [editing, setEditing] = useState(!!init);
  const [shown, setShown] = useState(null);       // saved text until the new state arrives
  const [error, setError] = useState(null);
  const base = useRef(init ? init.base : null), busy = useRef(false), from = useRef(init ? init.v : null);
  useEffect(() => setShown(null), [text]);
  const start = () => { base.current = from.current = shown ?? text; setEditing(true); };
  const keep = (v) => dk && saveDraft(dk, v === base.current ? null : { v, base: base.current });
  const finish = async (v) => {
    if (busy.current) return;
    if (v === base.current) { keep(v); setEditing(false); setError(null); return; }
    busy.current = true;
    try {
      await save(v, base.current);
      base.current = v;                 // a late blur finishes again: nothing to save
      keep(v); setShown(v); setEditing(false); setError(null);
    } catch (e) { keep(v); setError(e.message); }
    finally { busy.current = false; }
  };
  return { editing, start, shown: shown ?? text, from: from.current, restored: !!init && from.current === init.v, error, finish, keep };
}

const lockTitle = "the running task can't be changed until its run ends";

export const EditableBody = (p) => html`<${Body} key=${p.draft} ...${p} />`;
function Body({ text, save, draft, disabled = false, empty = "No body.", ph = "Markdown" }) {
  const s = useSave(text, save, draft);
  const t = useEditTrigger(s.start, disabled || !save);
  if (s.editing) return html`<div class="editing">
    <${CodeEditor} value=${s.from} onDone=${s.finish} onChange=${s.keep} autofocus=${!s.restored} ph=${ph} />
    <div class="edit-hint">${s.error ? html`<span class="err">${s.error}</span>` : s.restored ? "Unsaved edit · Esc or click outside: save" : "Markdown supported · paste or drop images · Esc or click outside: save · Ctrl-Z: undo"}</div></div>`;
  return html`<div class=${"card editable" + (disabled ? " locked" : "")} tabindex="0" ref=${t.ref} onKeyDown=${t.onKeyDown}
    onClick=${t.onClick} title=${disabled ? lockTitle : save ? "i: edit" : ""}><${Markdown} text=${s.shown} empty=${empty} /></div>`;
}

export const EditableLine = (p) => html`<${Line} key=${p.draft} ...${p} />`;
function Line({ text, save, draft, disabled = false, tag = "h1" }) {
  const s = useSave(text, save, draft);
  const t = useEditTrigger(s.start, disabled || !save);
  if (s.editing) {
    const key = (e) => { if (e.key === "Escape" || e.key === "Enter") { e.preventDefault(); s.finish(e.target.value.trim()); } };
    return html`<div class="editing"><input class=${"line-edit " + tag} ref=${s.restored ? null : autofocus} value=${s.from} onKeyDown=${key}
      onInput=${(e) => s.keep(e.target.value.trim())} onBlur=${(e) => s.finish(e.target.value.trim())} />
      ${s.error ? html`<div class="edit-hint"><span class="err">${s.error}</span></div>` : null}</div>`;
  }
  const T = tag;
  return html`<${T} class=${"editable line" + (disabled ? " locked" : "")} tabindex="0" ref=${t.ref} onKeyDown=${t.onKeyDown}
    onClick=${t.onClick} title=${disabled ? lockTitle : save ? "i: edit" : ""}>${s.shown}</${T}>`;
}

// A priority: click or Enter to change; empty clears it. `from` says where an unset one comes from.
export function PrioEdit({ value, effective, from, save, disabled, what = "priority" }) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState(null);
  const finish = async (raw) => {
    const v = raw.trim() === "" ? null : Number(raw);
    if (v !== null && !Number.isInteger(v)) { setError("a whole number, or empty"); return; }
    if (v === value) { setEditing(false); return; }
    try { await save(v); setEditing(false); setError(null); } catch (e) { setError(e.message); }
  };
  if (editing) return html`<span class="prio-edit"><input ref=${autofocus} value=${value ?? ""} placeholder=${String(effective)}
    onKeyDown=${(e) => { if (e.key === "Enter" || e.key === "Escape") finish(e.target.value); }} onBlur=${(e) => finish(e.target.value)} />
    ${error ? html`<span class="err">${error}</span>` : null}</span>`;
  const label = value != null ? html`${value}` : from === "plan" ? html`<span class="inherited">${effective}</span>` : html`${effective}<span class="from"> ${from}</span>`;
  if (!save || disabled) return html`<span class="prio" title=${disabled ? lockTitle : ""}>${label}</span>`;
  return html`<button class="prio prio-btn" title=${`change the ${what} (empty: ${what === "priority" ? "none" : "0"})`} onClick=${(e) => { e.preventDefault(); e.stopPropagation(); setEditing(true); }}>${label}</button>`;
}
