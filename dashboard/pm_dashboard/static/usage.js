// The status line: the Claude account's 5-hour and weekly limits (from the workers' last rate_limit_event) and the
// tokens pm's workers used in each window.
import { html, useState, useEffect } from "/vendor/preact-htm.js";
import { tokens, usd, ago } from "/data.js";

const WINDOWS = [["five_hour", "5-hour window", 5 * 3600e3], ["seven_day", "week", 7 * 86400e3]];

function useUsage(version) {
  const [u, setU] = useState(null);
  useEffect(() => {
    let gone = false;
    const load = () => fetch("/api/usage").then((r) => r.json()).then((j) => !gone && setU(j)).catch(() => {});
    load();
    const t = setInterval(load, 30e3);
    return () => { gone = true; clearInterval(t); };
  }, [version]);
  return u;
}

const resets = (ms) => {
  const d = new Date(ms), p = (n) => String(n).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return d - Date.now() < 86400e3 && d.getDate() === new Date().getDate() ? hm
    : `${d.toLocaleDateString(undefined, { weekday: "short" })} ${hm}`;
};

function Window({ d, label, w, span, children }) {
  const now = Date.now();
  const end = w && w.resetsAt ? w.resetsAt * 1000 : null;
  const live = end && end > now;
  const start = live ? end - span : now - span;
  const runs = d.runs.filter((r) => r.started_at && new Date(r.started_at) >= start);
  const n = runs.reduce((s, r) => s + d.tokens(r), 0);
  const pct = live && w.utilization != null ? Math.round(w.utilization * 100) : null;
  const elapsed = Math.round(Math.min(1, Math.max(0, (now - start) / span)) * 100);
  const ahead = pct != null && pct > elapsed;
  return html`<span class="usage-win" title=${live ? `${elapsed} % of the window passed · resets ${new Date(end).toLocaleString()}\n${ahead ? "used faster than time passes: runs out before the reset" : "used slower than time passes: budget left at the reset"}` : "reset since the last worker run; usage unknown"}>
    <b>${label}</b>
    ${pct != null ? html`<span class=${"meter" + (ahead ? " ahead" : "")}><span style=${`width:${Math.min(100, pct)}%`}></span><i style=${`left:${elapsed}%`}></i></span>
      <span class="num">${pct} %</span>${children}<span class="muted">· resets ${resets(end)}</span>`
      : html`<span class="muted">${end ? "reset since" : "limit unknown"}</span>`}
    <span class="muted">· pm workers ${tokens(n) || "0"} tokens${runs.length ? `, ${usd(d.cost(runs))}` : ""}</span></span>`;
}

// Weekly limit used per hour over the last hour against what lasts the week: green and still while within it,
// a ticking yellow (up to twice) or orange clock above it.
const WEEK_RATE = 1 / 168;

function BurnClock({ b }) {
  if (!b) return null;
  const x = b.rate == null ? null : b.rate / WEEK_RATE;
  const level = x == null || x <= 1 ? "" : x <= 2 ? " fast" : " faster";
  const title = x == null ? "burn rate: measuring, less than 15 minutes of worker data in the last hour"
    : !b.since ? "burn rate: no worker activity in the last hour"
    : `burn rate over the last hour: ${(b.rate * 100).toFixed(2)} % of the week per hour, ${x.toFixed(1)}× what lasts the week (${(WEEK_RATE * 100).toFixed(2)} %/h)`;
  return html`<svg class=${"burn" + level} viewBox="0 0 16 16" width="14" height="14" role="img" aria-label=${title}><title>${title}</title>
    <circle cx="8" cy="8" r="6.5" /><line class="h" x1="8" y1="8" x2="8" y2="4.5" /><line class="m" x1="8" y1="8" x2="11" y2="8" /></svg>`;
}

export function UsageLine({ d, version }) {
  const u = useUsage(version);
  if (!d) return null;
  const e = u && u.event;
  return html`<footer class="usage">
    <span class="muted">Claude usage</span>
    ${WINDOWS.map(([k, label, span]) => html`<${Window} d=${d} label=${label} w=${e && e.windows[k]} span=${span}>
      ${k === "seven_day" && html`<${BurnClock} b=${u && u.burn} />`}</${Window}>`)}
    <span class="grow"></span>
    ${e ? html`<span class="muted" title="from the last rate_limit_event a pm worker logged">${e.status && e.status !== "allowed" ? html`<b class="err">${e.status}</b> · ` : ""}${e.overage ? "using extra usage · " : ""}limits as of ${ago(e.at)}</span>`
      : u ? html`<span class="muted">limits show after the first <code>pm run</code> worker</span>` : null}
  </footer>`;
}
