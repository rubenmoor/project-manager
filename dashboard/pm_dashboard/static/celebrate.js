// A milestone implemented: a message and fireworks, shown once the tab is visible; stays until "Yay!".
import { html, useState, useEffect } from "/vendor/preact-htm.js";

const key = (root) => `pm-celebrated:${root}`;
const load = (root) => { try { const s = localStorage.getItem(key(root)); return s ? new Set(JSON.parse(s)) : null; } catch { return null; } };
const save = (root, ids) => { try { localStorage.setItem(key(root), JSON.stringify([...ids])); } catch {} };

function useVisible() {
  const [visible, setVisible] = useState(!document.hidden);
  useEffect(() => {
    const f = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", f);
    return () => document.removeEventListener("visibilitychange", f);
  }, []);
  return visible;
}

function fireworks(canvas) {
  const ctx = canvas.getContext("2d");
  const dpr = devicePixelRatio || 1;
  const size = () => { canvas.width = innerWidth * dpr; canvas.height = innerHeight * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); };
  size();
  addEventListener("resize", size);
  const colors = ["#2a78d6", "#0ca30c", "#fab219", "#ec835a", "#d03b3b", "#9b5de5", "#00bbf9"];
  const rockets = [], sparks = [];
  const start = performance.now(), launchFor = 4500;
  let next = 0, frame;
  const launch = () => rockets.push({ x: innerWidth * (0.15 + Math.random() * 0.7), y: innerHeight, vx: (Math.random() - 0.5) * 2,
    vy: -Math.sqrt(0.3 * innerHeight * (0.5 + Math.random() * 0.35)), color: colors[Math.floor(Math.random() * colors.length)] });
  const burst = (r) => {
    const n = 60 + Math.floor(Math.random() * 40), speed = 2 + Math.random() * 3;
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2, v = speed * (0.6 + Math.random() * 0.4);
      sparks.push({ x: r.x, y: r.y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 1, decay: 0.012 + Math.random() * 0.01,
        color: Math.random() < 0.2 ? colors[Math.floor(Math.random() * colors.length)] : r.color });
    }
  };
  const tick = (t) => {
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    if (t - start < launchFor && t >= next) { launch(); if (Math.random() < 0.4) launch(); next = t + 250 + Math.random() * 350; }
    for (let i = rockets.length - 1; i >= 0; i--) {
      const r = rockets[i];
      r.x += r.vx; r.y += r.vy; r.vy += 0.15;
      ctx.fillStyle = r.color;
      ctx.fillRect(r.x - 1.5, r.y - 4, 3, 8);
      if (r.vy >= -1) { burst(r); rockets.splice(i, 1); }
    }
    for (let i = sparks.length - 1; i >= 0; i--) {
      const s = sparks[i];
      s.x += s.vx; s.y += s.vy; s.vx *= 0.98; s.vy = s.vy * 0.98 + 0.05; s.life -= s.decay;
      if (s.life <= 0) { sparks.splice(i, 1); continue; }
      ctx.globalAlpha = s.life;
      ctx.fillStyle = s.color;
      ctx.beginPath(); ctx.arc(s.x, s.y, 2.2, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (t - start < launchFor || rockets.length || sparks.length) frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
  return () => { cancelAnimationFrame(frame); removeEventListener("resize", size); };
}

function Fireworks() {
  const [canvas, setCanvas] = useState(null);
  useEffect(() => {
    if (!canvas || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    return fireworks(canvas);
  }, [canvas]);
  return html`<canvas class="fireworks" ref=${setCanvas}></canvas>`;
}

export function Celebrate({ d }) {
  const root = d ? d.state.root : null;
  const [seen, setSeen] = useState(null);
  const visible = useVisible();
  useEffect(() => {
    if (!root) return;
    let s = load(root);
    // First visit to this project: nothing earlier is news.
    if (!s) { s = new Set(d.milestones.filter((m) => m.status === "implemented").map((m) => m.id)); save(root, s); }
    setSeen(s);
  }, [root]);
  if (!d || !seen) return null;
  const fresh = d.milestones.filter((m) => m.status === "implemented" && !seen.has(m.id));
  if (!fresh.length || !visible) return null;
  const yay = () => { const s = new Set([...seen, ...fresh.map((m) => m.id)]); save(root, s); setSeen(s); };
  return html`<div class="celebrate">
    <${Fireworks} />
    <div class="celebrate-card" role="dialog" aria-label="milestone implemented">
      <div class="celebrate-title">${fresh.length > 1 ? `${fresh.length} milestones implemented` : "Milestone implemented"}</div>
      ${fresh.map((m) => html`<a class="celebrate-subject" href=${`#/milestone/${m.id}`} onClick=${yay}><span class="mono">m${m.id}</span> ${m.subject}</a>`)}
      <button class="btn primary big" autofocus onClick=${yay}>Yay!</button>
    </div>
  </div>`;
}
