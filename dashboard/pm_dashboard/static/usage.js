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

function Window({ d, label, w, span }) {
  const now = Date.now();
  const end = w && w.resetsAt ? w.resetsAt * 1000 : null;
  const live = end && end > now;
  const start = live ? end - span : now - span;
  const runs = d.runs.filter((r) => r.started_at && new Date(r.started_at) >= start);
  const n = runs.reduce((s, r) => s + d.tokens(r), 0);
  const pct = live && w.utilization != null ? Math.round(w.utilization * 100) : null;
  return html`<span class="usage-win" title=${live ? `resets ${new Date(end).toLocaleString()}` : "reset since the last worker run; usage unknown"}>
    <b>${label}</b>
    ${pct != null ? html`<span class=${"meter" + (pct >= 90 ? " bad" : pct >= 75 ? " warn" : "")}><span style=${`width:${Math.min(100, pct)}%`}></span></span>
      <span class="num">${pct} %</span><span class="muted">· resets ${resets(end)}</span>`
      : html`<span class="muted">${end ? "reset since" : "limit unknown"}</span>`}
    <span class="muted">· pm workers ${tokens(n) || "0"} tokens${runs.length ? `, ${usd(d.cost(runs))}` : ""}</span></span>`;
}

export function UsageLine({ d, version }) {
  const u = useUsage(version);
  if (!d) return null;
  const e = u && u.event;
  return html`<footer class="usage">
    <span class="muted">Claude usage</span>
    ${WINDOWS.map(([k, label, span]) => html`<${Window} d=${d} label=${label} w=${e && e.windows[k]} span=${span} />`)}
    <span class="grow"></span>
    ${e ? html`<span class="muted" title="from the last rate_limit_event a pm worker logged">${e.status && e.status !== "allowed" ? html`<b class="err">${e.status}</b> · ` : ""}${e.overage ? "using extra usage · " : ""}limits as of ${ago(e.at)}</span>`
      : u ? html`<span class="muted">limits show after the first <code>pm run</code> worker</span>` : null}
  </footer>`;
}
