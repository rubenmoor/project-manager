// The state from /api/state, indexed, with what pm derives: effective priorities, blocking decisions, runs per task.
export function index(state) {
  const t = state.tables;
  const by = (rows) => new Map(rows.map((r) => [r.id, r]));
  const d = {
    state, milestones: t.milestone, plans: t.plan, tasks: t.task, decisions: t.decision, runs: t.run,
    milestone: by(t.milestone), plan: by(t.plan), task: by(t.task), decision: by(t.decision),
    queue: state.queue, queuePos: new Map(state.queue.map((id, i) => [id, i])),
    running: t.run.find((r) => r.result === "running") || null,
    planDone: new Set(state.done_plans || []),
  };
  const group = (rows, key) => {
    const m = new Map();
    for (const r of rows) (m.get(r[key]) || m.set(r[key], []).get(r[key])).push(r);
    return m;
  };
  d.plansOf = group(t.plan, "milestone_id");
  d.tasksOf = group(t.task, "plan_id");
  d.runsOf = group(t.run, "task_id");
  d.decisionsOf = new Map();
  d.tasksWaiting = new Map();
  for (const l of t.task_decision) {
    (d.decisionsOf.get(l.task_id) || d.decisionsOf.set(l.task_id, []).get(l.task_id)).push(d.decision.get(l.decision_id));
    (d.tasksWaiting.get(l.decision_id) || d.tasksWaiting.set(l.decision_id, []).get(l.decision_id)).push(d.task.get(l.task_id));
  }
  d.createdBy = new Map();
  for (const [kind, rows] of [["plan", t.plan], ["task", t.task], ["decision", t.decision]])
    for (const r of rows)
      if (r.created_by_task != null)
        (d.createdBy.get(r.created_by_task) || d.createdBy.set(r.created_by_task, []).get(r.created_by_task)).push([kind, r]);
  d.milestoneOfTask = (task) => d.milestone.get(d.plan.get(task.plan_id).milestone_id);
  d.prio = (task) => {
    if (task.priority != null) return { value: task.priority, from: "task" };
    const p = d.plan.get(task.plan_id);
    return p.priority != null ? { value: p.priority, from: "plan" } : { value: 0, from: "default" };
  };
  // Pending decisions an open task of milestone m waits for: the ones the milestone needs answered to finish.
  d.milestoneDecisions = (m) => d.decisions.filter((x) => x.status === "pending"
    && (d.tasksWaiting.get(x.id) || []).some((t) => t.status === "open" && d.milestoneOfTask(t).id === m.id));
  d.blockers = (task) => (d.decisionsOf.get(task.id) || []).filter((x) => x.status === "pending");
  d.excluded = (task) => {
    if (task.status !== "open") return null;
    const m = d.milestoneOfTask(task);
    if (m.status !== "open") return `milestone ${m.id} is ${m.status.replace("-", " ")}`;
    const b = d.blockers(task);
    return b.length ? b : null;
  };
  d.held = (m) => m.status === "on-hold";
  d.taskState = (task) => (task.status === "open" && d.held(d.milestoneOfTask(task)) ? "on-hold" : task.status);
  d.cost = (runs) => runs.reduce((s, r) => s + (r.cost_usd || 0), 0);
  d.tokens = (r) => (r.input_tokens || 0) + (r.output_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_creation_tokens || 0);
  d.taskRuns = (task) => d.runsOf.get(task.id) || [];
  d.milestoneTasks = (m) => (d.plansOf.get(m.id) || []).flatMap((p) => d.tasksOf.get(p.id) || []);
  d.plansSorted = (m) => [...(d.plansOf.get(m.id) || [])].sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || a.id - b.id);
  return d;
}

export const parseTs = (ts) => (ts ? new Date(ts) : null);

export function seconds(start, end) {
  return Math.max(0, Math.round(((end ? parseTs(end) : new Date()) - parseTs(start)) / 1000));
}

export function duration(secs) {
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60), s = secs % 60;
  if (m < 60) return `${m}m${String(s).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}

export function ago(ts) {
  if (!ts) return "";
  const s = seconds(ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function stamp(ts) {
  if (!ts) return "";
  const d = parseTs(ts);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export const usd = (x) => (x == null ? "" : x >= 100 ? `$${x.toFixed(0)}` : `$${x.toFixed(2)}`);

export function tokens(n) {
  if (!n) return "";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

export const day = (ts) => {
  const d = parseTs(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
