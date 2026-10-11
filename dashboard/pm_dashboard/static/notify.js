// Browser notifications: a new pending decision, a failed run, `pm run` stopped with work left, the queue done.
import { html, useState, useEffect, useRef } from "/vendor/preact-htm.js";

const KEY = "pm-notify";
const on = () => { try { return localStorage.getItem(KEY) === "1" && Notification.permission === "granted"; } catch { return false; } };

function notify(title, body, hash) {
  if (!on()) return;
  const n = new Notification(title, { body, tag: title });
  n.onclick = () => { focus(); if (hash) location.hash = hash; n.close(); };
}

export function useNotifications(d) {
  const prev = useRef(null);
  const stopTimer = useRef(null);
  useEffect(() => {
    if (!d) return;
    const now = {
      pending: new Set(d.decisions.filter((x) => x.status === "pending").map((x) => x.id)),
      failed: new Set(d.runs.filter((x) => x.result === "failed").map((x) => x.id)),
      running: d.running ? d.running.id : null,
      queue: d.queue.length,
    };
    const p = prev.current;
    prev.current = now;
    if (!p) return;
    const fresh = [...now.pending].filter((id) => !p.pending.has(id));
    if (fresh.length) {
      const x = d.decision.get(fresh[0]);
      notify(fresh.length > 1 ? `${fresh.length} new decisions` : x.signoff ? "Sign-off waiting" : "Decision waiting", x.subject, `#/decision/${x.id}`);
    }
    for (const id of now.failed) if (!p.failed.has(id)) {
      const r = d.runs.find((x) => x.id === id);
      notify(`Task ${r.task_id} failed`, (r.reason || "").split("\n")[0], `#/task/${r.task_id}`);
    }
    if (now.running) clearTimeout(stopTimer.current);
    else if (p.running) {
      // Between two tasks nothing runs for a moment; only a lasting pause means pm run stopped.
      stopTimer.current = setTimeout(() => {
        if (now.queue) notify("pm run stopped", `${now.queue} tasks left in the queue`, "#/live");
        else notify("Queue done", "pm run worked every task it could", "#/overview");
      }, 15000);
    }
  }, [d]);
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
