// Browser notifications, from every project: a new pending decision, a failed run, `pm run` stopped with work left, the queue done.
import { html, useState, useEffect, useRef } from "/vendor/preact-htm.js";
import { PROJECT, projectUrl } from "/projects.js";

const KEY = "pm-notify";
const on = () => { try { return localStorage.getItem(KEY) === "1" && Notification.permission === "granted"; } catch { return false; } };

function notify(project, title, body, hash) {
  if (!on()) return;
  const here = project === PROJECT;
  const n = new Notification(here ? title : `${project}: ${title}`, { body, tag: `${project}:${title}` });
  n.onclick = () => { focus(); if (here) { if (hash) location.hash = hash; } else location.href = projectUrl(project, hash || ""); n.close(); };
}

// From every project's summary (/api/projects, kept current by the event stream).
export function useNotifications(projects) {
  const prev = useRef(new Map());
  const stopTimer = useRef(new Map());
  useEffect(() => {
    if (!projects) return;
    for (const x of projects) {
      if (x.error && !x.pending) continue;
      const now = {
        pending: new Set(x.pending.map((d) => d.id)),
        failed: new Set(x.failed.map((r) => r.id)),
        running: x.running.length,
        crash: x.crash ? x.crash.at : null,
        queue: x.queue,
      };
      const p = prev.current.get(x.name);
      prev.current.set(x.name, now);
      if (!p) continue;
      const say = (title, body, hash) => notify(x.name, title, body, hash);
      const fresh = x.pending.filter((d) => !p.pending.has(d.id));
      if (fresh.length) {
        const d = fresh[0];
        say(fresh.length > 1 ? `${fresh.length} new decisions` : d.signoff ? "Sign-off waiting" : "Decision waiting", d.subject, `#/decision/${d.id}`);
      }
      for (const r of x.failed) if (!p.failed.has(r.id) && (!p.failed.size || r.id > Math.max(...p.failed)))
        say(`Task ${r.task} failed`, r.reason, `#/task/${r.task}`);
      const timers = stopTimer.current;
      if (now.crash && now.crash !== p.crash) {
        clearTimeout(timers.get(x.name));
        say("pm run crashed", x.crash.error || "no error message", "#/live");
      } else if (now.running) clearTimeout(timers.get(x.name));
      else if (p.running) {
        // Between two tasks nothing runs for a moment; only a lasting pause means pm run stopped.
        timers.set(x.name, setTimeout(() => {
          if (now.queue) say("pm run stopped", `${now.queue} tasks left in the queue`, "#/live");
          else say("Queue done", "pm run worked every task it could", "#/overview");
        }, 15000));
      }
    }
  }, [projects]);
}

export function NotifyButton() {
  const supported = typeof Notification !== "undefined";
  const [state, setState] = useState(on());
  if (!supported) return null;
  const toggle = async () => {
    if (state) { try { localStorage.setItem(KEY, "0"); } catch {} setState(false); return; }
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm === "granted") { try { localStorage.setItem(KEY, "1"); } catch {} setState(true); }
  };
  return html`<button class="iconbtn" title=${state ? "notifications on: decisions, failures, pm run stopping" : "turn on notifications"}
    onClick=${toggle}>${state ? "notify: on" : "notify: off"}</button>`;
}
