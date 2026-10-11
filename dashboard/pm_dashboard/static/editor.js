// Edit in place: a text shows rendered; `i`, Enter or a click shows its source (CodeMirror, monospace);
// Esc or leaving it saves and renders it again.
import { html, useState, useEffect, useRef } from "/vendor/preact-htm.js";
import {
  EditorView, EditorState, keymap, placeholder, drawSelection, history, historyKeymap, defaultKeymap, indentWithTab,
  markdown, markdownLanguage, syntaxHighlighting, HighlightStyle, tags, closeBrackets, closeBracketsKeymap,
  searchKeymap, highlightSelectionMatches,
} from "/vendor/codemirror.js";
import { Markdown } from "/ui.js";

// A ref that focuses and selects the element as soon as it is mounted.
export const autofocus = (el) => {
  if (el && !el.dataset.focused) { el.dataset.focused = "1"; setTimeout(() => { el.focus(); el.select && el.select(); }); }
};

export async function post(op) {
  const r = await fetch("/api/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op) });
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

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
          EditorView.domEventHandlers({ blur: (e, v) => { if (!box.current.contains(e.relatedTarget)) finish(v); } }),
          EditorView.updateListener.of((u) => u.docChanged && change.current && change.current(u.state.doc.toString())),
        ],
      }),
    });
    if (autofocus) { view.focus(); view.dispatch({ selection: { anchor: view.state.doc.length } }); }
    return () => view.destroy();
  }, []);
  return html`<div class="cm-box" ref=${box}></div>`;
}

// Starts editing on `i` / Enter / click (not on a link), or on the "pm-edit" event (the global `i` key).
function useEditTrigger(start, disabled) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const go = () => !disabled && start();
    el.addEventListener("pm-edit", go);
    return () => el.removeEventListener("pm-edit", go);
  });
  const onKeyDown = (e) => {
    if (!disabled && (e.key === "i" || e.key === "Enter") && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); start(); }
  };
  const onClick = (e) => { if (!disabled && !e.target.closest("a") && !getSelection().toString()) start(); };
  return { ref, onKeyDown, onClick };
}

function useSave(text, save) {
  const [editing, setEditing] = useState(false);
  const [shown, setShown] = useState(null);       // saved text until the new state arrives
  const [error, setError] = useState(null);
  useEffect(() => setShown(null), [text]);
  const finish = async (v) => {
    if (v === (shown ?? text)) { setEditing(false); setError(null); return; }
    try {
      await save(v);
      setShown(v); setEditing(false); setError(null);
    } catch (e) { setError(e.message); }
  };
  return { editing, setEditing, shown: shown ?? text, error, finish };
}

const lockTitle = "the running task can't be changed until its run ends";

export function EditableBody({ text, save, disabled = false, empty = "No body.", ph = "Markdown" }) {
  const s = useSave(text, save);
  const t = useEditTrigger(() => s.setEditing(true), disabled || !save);
  if (s.editing) return html`<div class="editing">
    <${CodeEditor} value=${s.shown} onDone=${s.finish} ph=${ph} />
    <div class="edit-hint">${s.error ? html`<span class="err">${s.error}</span>` : "Esc or click outside: save · Ctrl-Z: undo"}</div></div>`;
  return html`<div class=${"card editable" + (disabled ? " locked" : "")} tabindex="0" ref=${t.ref} onKeyDown=${t.onKeyDown}
    onClick=${t.onClick} title=${disabled ? lockTitle : save ? "i: edit" : ""}><${Markdown} text=${s.shown} empty=${empty} /></div>`;
}

export function EditableLine({ text, save, disabled = false, tag = "h1" }) {
  const s = useSave(text, save);
  const t = useEditTrigger(() => s.setEditing(true), disabled || !save);
  if (s.editing) {
    const key = (e) => { if (e.key === "Escape" || e.key === "Enter") { e.preventDefault(); s.finish(e.target.value.trim()); } };
    return html`<div class="editing"><input class=${"line-edit " + tag} ref=${autofocus} value=${s.shown} onKeyDown=${key}
      onBlur=${(e) => s.finish(e.target.value.trim())} />
      ${s.error ? html`<div class="edit-hint"><span class="err">${s.error}</span></div>` : null}</div>`;
  }
  const T = tag;
  return html`<${T} class=${"editable line" + (disabled ? " locked" : "")} tabindex="0" ref=${t.ref} onKeyDown=${t.onKeyDown}
    onClick=${t.onClick} title=${disabled ? lockTitle : save ? "i: edit" : ""}>${s.shown}</${T}>`;
}

// A priority: click or Enter to change; empty clears it. `from` says where an unset one comes from.
export function PrioEdit({ value, effective, from, save, disabled }) {
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
  const label = value != null ? html`${value}` : html`${effective}<span class="from"> ${from}</span>`;
  if (!save || disabled) return html`<span class="prio" title=${disabled ? lockTitle : ""}>${label}</span>`;
  return html`<button class="prio prio-btn" title="change the priority (empty: none)" onClick=${(e) => { e.preventDefault(); e.stopPropagation(); setEditing(true); }}>${label}</button>`;
}

// Global `i`: edit the focused editable, else the first one on the page.
addEventListener("keydown", (e) => {
  if (e.key !== "i" || e.metaKey || e.ctrlKey || e.altKey) return;
  const a = document.activeElement;
  if (a && (/INPUT|TEXTAREA|SELECT/.test(a.tagName) || a.isContentEditable || a.closest(".editable"))) return;
  const el = document.querySelector(".main .card.editable:not(.locked)") || document.querySelector(".main .editable:not(.locked)");
  if (el) { e.preventDefault(); el.dispatchEvent(new Event("pm-edit")); }
});
