import { html, useState, useEffect, useMemo, useRef } from "/vendor/preact-htm.js";
import { marked } from "/vendor/marked.js";
import { ago, stamp, duration, seconds } from "/data.js";

const escape = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// Bodies are written by workers: raw HTML in them is shown as text, never run.
marked.use({ gfm: true, breaks: false, renderer: { html: (h) => escape(typeof h === "string" ? h : h.text) } });

export function Markdown({ text, empty = "No body." }) {
  const out = useMemo(() => (text && text.trim() ? marked.parse(text) : ""), [text]);
  if (!out) return html`<div class="empty">${empty}</div>`;
  return html`<div class="md" dangerouslySetInnerHTML=${{ __html: out }}></div>`;
}

export const Pill = ({ status, label }) => html`<span class="pill ${status}"><span class="dot"></span>${label || status.replace("-", " ")}</span>`;
export const Kind = ({ kind }) => (kind && kind !== "work" ? html`<span class="tag">${kind}</span>` : null);

export function Prio({ p }) {
  return html`<span class="prio" title=${`priority ${p.value}, ${p.from === "task" ? "the task's own" : p.from === "plan" ? "from its plan" : "no priority set"}`}>
    ${p.from === "plan" ? html`<span class="inherited">${p.value}</span>` : p.value}${p.from === "default" ? html`<span class="from"> –</span>` : null}</span>`;
}

// The id prefix shown before every task, plan and milestone subject: t12, p3, m1.
export const Ref = ({ kind, id }) => html`<span class="ref">${kind[0]}${id}</span>`;
export const Named = ({ kind, r }) => html`<${Ref} kind=${kind} id=${r.id} /> ${r.subject}`;
export const NamedLink = ({ kind, r }) => html`<a href=${`#/${kind}/${r.id}`}><${Named} kind=${kind} r=${r} /></a>`;
export const TaskLink = ({ t }) => html`<${NamedLink} kind="task" r=${t} />`;
export const Titled = ({ kind, id, children }) => html`<div class="titled"><${Ref} kind=${kind} id=${id} />${children}</div>`;
export const Id = ({ kind, id }) => html`<a class="mono muted" href=${`#/${kind}/${id}`}>${kind[0]}${id}</a>`;

export function Time({ ts }) {
  if (!ts) return null;
  return html`<span title=${stamp(ts) + " (local)"}>${ago(ts)}</span>`;
}

// Re-renders every second while mounted, for running clocks.
export function useTick(on = true) {
  const [, set] = useState(0);
  useEffect(() => {
    if (!on) return;
    const i = setInterval(() => set((x) => x + 1), 1000);
    return () => clearInterval(i);
  }, [on]);
}

export function Elapsed({ start, end }) {
  useTick(!end);
  return html`<span class="num">${duration(seconds(start, end))}</span>`;
}

export const Tile = ({ label, value, sub }) => html`<div class="card tile"><div class="label">${label}</div>
  <div class="value num">${value}</div>${sub ? html`<div class="sub">${sub}</div>` : null}</div>`;

export function Progress({ done, dropped, open }) {
  const n = done + dropped + open || 1;
  return html`<div class="bar" role="img" aria-label=${`${done} done, ${dropped} dropped, ${open} open`}>
    ${done ? html`<span class="done" style=${`width:${(100 * done) / n}%`}></span>` : null}
    ${dropped ? html`<span class="dropped" style=${`width:${(100 * dropped) / n}%`}></span>` : null}
  </div>
  <div class="legend"><span><i style="background:var(--series-1)"></i>${done} done</span>
    <span><i style="background:var(--dropped)"></i>${dropped} dropped</span>
    <span><i style="background:var(--track)"></i>${open} open</span></div>`;
}

// The element's width in CSS pixels, so SVG text keeps its size.
function useWidth(initial = 600) {
  const ref = useRef(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    const o = new ResizeObserver(([e]) => setW(Math.max(200, Math.floor(e.contentRect.width))));
    if (ref.current) o.observe(ref.current);
    return () => o.disconnect();
  }, []);
  return [ref, w];
}

function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v)), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

// One series of bars over categories (x: label), with a hover tooltip per bar.
export function Bars({ data, fmt = String, height = 180, horizontal = false }) {
  const [tip, setTip] = useState(null);
  const [ref, W] = useWidth();
  if (!data.length) return html`<div class="empty">No data yet.</div>`;
  const max = horizontal ? Math.max(...data.map((d) => d.value)) || 1 : niceMax(Math.max(...data.map((d) => d.value)) || 1);
  if (horizontal) {
    const label = 120, row = 26, H = data.length * row + 4;
    const x = (v) => ((W - label - 70) * v) / max;
    return html`<div class="chart" ref=${ref}><svg width=${W} height=${H} viewBox=${`0 0 ${W} ${H}`} role="img">
      ${data.map((d, i) => html`<g>
        <text class="lbl" x=${label - 8} y=${i * row + 17} text-anchor="end">${d.label}</text>
        <rect class="mark" x=${label} y=${i * row + 6} width=${Math.max(2, x(d.value))} height="14" rx="3"></rect>
        <text class="val" x=${label + Math.max(2, x(d.value)) + 6} y=${i * row + 17}>${fmt(d.value)}</text>
      </g>`)}
      <line class="baseline" x1=${label} x2=${label} y1="0" y2=${H}></line>
    </svg></div>`;
  }
  const H = height, pad = { l: 44, r: 8, t: 10, b: 22 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const step = iw / data.length, bw = Math.max(2, Math.min(28, step - 2));
  const ticks = [0, max / 2, max];
  const y = (v) => pad.t + ih - (ih * v) / max;
  const every = Math.ceil(data.length / Math.max(2, Math.floor(iw / 56)));
  return html`<div class="chart" ref=${ref} onMouseLeave=${() => setTip(null)}>
    <svg width=${W} height=${H} viewBox=${`0 0 ${W} ${H}`} role="img">
      ${ticks.map((t) => html`<g><line class="gridline" x1=${pad.l} x2=${W - pad.r} y1=${y(t)} y2=${y(t)}></line>
        <text class="tick" x=${pad.l - 6} y=${y(t) + 4} text-anchor="end">${fmt(t)}</text></g>`)}
      ${data.map((d, i) => {
        const x = pad.l + i * step + (step - bw) / 2, h = Math.max(d.value ? 2 : 0, (ih * d.value) / max);
        return html`<g>
          <rect class="hit" x=${pad.l + i * step} y=${pad.t} width=${step} height=${ih}
            onMouseEnter=${() => setTip({ i, left: ((x + bw / 2) / W) * 100, top: (y(d.value) / H) * 100 })}></rect>
          <rect class=${"mark" + (tip && tip.i === i ? " hl" : "")} x=${x} y=${pad.t + ih - h} width=${bw} height=${h} rx="3"
            pointer-events="none"></rect>
          ${i % every === 0 ? html`<text class="tick" x=${x + bw / 2} y=${H - 6} text-anchor="middle">${d.short || d.label}</text>` : null}
        </g>`;
      })}
      <line class="baseline" x1=${pad.l} x2=${W - pad.r} y1=${pad.t + ih} y2=${pad.t + ih}></line>
    </svg>
    ${tip ? html`<div class="tip" style=${`left:${tip.left}%;top:${tip.top}%`}>
      <b>${fmt(data[tip.i].value)}</b> <span class="muted">${data[tip.i].label}</span>
      ${data[tip.i].extra ? html`<div class="muted">${data[tip.i].extra}</div>` : null}</div>` : null}
  </div>`;
}

// A section that starts hidden; `head` is its heading.
export function Fold({ head, open = false, children }) {
  const [shown, setShown] = useState(open);
  useEffect(() => setShown(open), [open]);
  return html`<div class="section-head">${head}
      <button class="iconbtn" onClick=${() => setShown(!shown)}>${shown ? "hide" : "show"}</button></div>
    ${shown ? children : null}`;
}
