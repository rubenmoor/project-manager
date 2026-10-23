// Several projects: the one shown (from the URL, /p/<name>/), the switcher in the top bar, adding and removing projects.
import { html, useState, useEffect, useRef } from "/vendor/preact-htm.js";

const m = location.pathname.match(/^\/p\/([^/]+)/);
export const PROJECT = m ? decodeURIComponent(m[1]) : null;
export const projectUrl = (name, hash = "") => `/p/${encodeURIComponent(name)}/${hash}`;

async function call(op) {
  const r = await fetch("/api/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op) });
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

export function AddProject({ autofocus }) {
  const [path, setPath] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const input = useRef(null);
  useEffect(() => { if (autofocus && input.current) input.current.focus(); }, []);
  const add = async (e) => {
    e.preventDefault();
    if (!path.trim()) return;
    setBusy(true);
    try { const r = await call({ op: "add", path: path.trim() }); location.href = projectUrl(r.name); }
    catch (err) { setError(err.message); setBusy(false); }
  };
  return html`<form class="addproject" onSubmit=${add}>
    <input ref=${input} placeholder="/path/to/repository" value=${path} onInput=${(e) => { setPath(e.target.value); setError(null); }} />
    <button class="iconbtn" disabled=${busy}>Add project</button>
    ${error ? html`<div class="error">${error}</div>` : null}
  </form>`;
}

function Badges({ p }) {
  return html`${p.error ? html`<span class="tag error" title=${p.error}>error</span>` : null}
    ${p.running && p.running.length ? html`<span class="livedot" title="a worker is running"></span>` : null}
    ${p.pending && p.pending.length ? html`<span class="tag decision" title="pending decisions">${p.pending.length}</span>` : null}`;
}

export function Switcher({ projects, current }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return;
    const f = (e) => { if (!box.current.contains(e.target)) setOpen(false); };
    addEventListener("mousedown", f);
    return () => removeEventListener("mousedown", f);
  }, [open]);
  const others = (projects || []).filter((p) => p.name !== PROJECT);
  const pending = others.reduce((n, p) => n + ((p.pending || []).length), 0);
  const running = others.some((p) => p.running && p.running.length);
  const remove = async (p) => {
    if (!confirm(`Remove ${p.name} from the dashboard? Its folder stays as it is.`)) return;
    try { await call({ op: "remove", name: p.name }); if (p.name === PROJECT) location.href = "/"; }
    catch (err) { alert(err.message); }
  };
  return html`<span class="switcher" ref=${box}>
    <button class="brand" title="switch or add projects" onClick=${() => setOpen(!open)}>pm<small>${current}</small>
      ${running ? html`<span class="livedot" title="a worker runs in another project"></span>` : null}
      ${pending ? html`<span class="tag decision" title="pending decisions in other projects">${pending}</span>` : null}
      <span class="caret">▾</span></button>
    ${open ? html`<div class="projects">
      ${(projects || []).map((p) => html`<div class=${"row" + (p.name === PROJECT ? " sel" : "")}>
        <a href=${projectUrl(p.name)} title=${p.root}><b>${p.name}</b> <span class="muted mono">${p.root}</span></a>
        <${Badges} p=${p} />
        <button class="remove" title="remove from the dashboard (the folder stays)" onClick=${() => remove(p)}>×</button>
      </div>`)}
      <${AddProject} autofocus />
    </div>` : null}
  </span>`;
}

export function NoProject() {
  return html`<div class="empty noproject"><p>No project yet. Add a pm project's git repository (one with project-manager.toml):</p>
    <${AddProject} autofocus /></div>`;
}
