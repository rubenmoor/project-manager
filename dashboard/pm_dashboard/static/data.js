// The state from /api/state, indexed, with what pm derives: effective priorities, blocking decisions, runs per task.
export function index(state) {
  const t = state.tables;
  const by = (rows) => new Map(rows.map((r) => [r.id, r]));
  const d = {
    state, milestones: t.milestone, plans: t.plan, tasks: t.task, decisions: t.decision, runs: t.run, summaries: t.summary || [],
    reports: t.resource_use_report || [], customs: t.custom_report || [],
    milestone: by(t.milestone), plan: by(t.plan), run: by(t.run), task: by(t.task), decision: by(t.decision),
    queue: state.queue, queuePos: new Map(state.queue.map((id, i) => [id, i])),
    // A 'running' row whose pm run is gone (it crashed) is stale, not running. One per work tree: the trunk's
    // first, then by work tree.
    runningAll: state.pm_run == null ? [] : t.run.filter((r) => r.result === "running")
      .sort((a, b) => (a.worktree || "").localeCompare(b.worktree || "")),
    stale: (state.pm_run == null && t.run.find((r) => r.result === "running")) || null,
    crash: state.pm_crash || null,
    planDone: new Set(state.done_plans || []),
  };
  d.running = d.runningAll[0] || null;
  // Why a queued task can't start (pm/__init__.py: start_tasks): a planning task above it (planning_holds), or, for the
  // trunk's next task, uncommitted changes in the trunk.
  d.holds = new Map(Object.entries(state.holds || {}).map(([k, v]) => [Number(k), v]));
  d.trunkDirty = (state.dirty || []).filter((f) => f !== state.db);
  d.trunkBlocked = d.trunkDirty.length > 0 && !d.runningAll.some((r) => !r.worktree);
  d.trunkHeld = d.trunkBlocked
    ? state.queue.map((id) => t.task.find((x) => x.id === id)).find((x) => x && !x.worktree && !d.holds.has(x.id)) || null : null;
  d.waitsFor = (x) => d.holds.has(x.id) ? `waits for t${d.holds.get(x.id)}, which plans above it${d.trunkBlocked ? " and waits for the trunk's uncommitted changes" : ""}`
    : d.trunkHeld && d.trunkHeld.id === x.id ? `waits: ${d.trunkDirty.length} uncommitted change(s) in the trunk` : null;
  d.runningOf = new Map(d.runningAll.map((r) => [r.task_id, r]));
  d.isRunning = (id) => d.runningOf.has(id);
  // rows grouped by key(row): Map key → [value(row)]
  const group = (rows, key, value = (r) => r) => {
    const m = new Map();
    for (const r of rows) {
      const k = key(r);
      (m.get(k) || m.set(k, []).get(k)).push(value(r));
    }
    return m;
  };
  d.plansOf = group(t.plan, (r) => r.milestone_id);
  d.tasksOf = group(t.task, (r) => r.plan_id);
  d.runsOf = group(t.run, (r) => r.task_id);
  d.decisionsOf = group(t.task_decision, (l) => l.task_id, (l) => d.decision.get(l.decision_id));
  d.tasksWaiting = group(t.task_decision, (l) => l.decision_id, (l) => d.task.get(l.task_id));
  // Prerequisite tasks (task_prerequisite_tasks): what a task requires, and the tasks that require it.
  const pre = t.task_prerequisite_tasks || [];
  d.requires = group(pre, (l) => l.task_id, (l) => d.task.get(l.prerequisite_id));
  d.requiredBy = group(pre, (l) => l.prerequisite_id, (l) => d.task.get(l.task_id));
  d.unmet = (task) => (d.requires.get(task.id) || []).filter((x) => x.status !== "done");
  d.stuck = (task) => task.status === "open" && d.unmet(task).some((x) => x.status === "dropped");
  // Supersede links: decision id → [{ id, note }] of the decisions it replaces, and of those replacing it.
  const sup = t.decision_supersedes || [];
  d.supersedes = group(sup, (l) => l.decision_id, (l) => ({ id: l.superseded_id, note: l.note }));
  d.supersededBy = group(sup, (l) => l.superseded_id, (l) => ({ id: l.decision_id, note: l.note }));
  // The decision log, as `pm decision list --answered` (pm decides: in_log).
  d.inLog = (x) => !!x.in_log;
  d.createdBy = group([["plan", t.plan], ["task", t.task], ["decision", t.decision]]
    .flatMap(([kind, rows]) => rows.filter((r) => r.created_by_task != null).map((r) => [kind, r])), ([, r]) => r.created_by_task);
  // A summary task has no plan, so no milestone: null.
  d.milestoneOfTask = (task) => (task.plan_id == null ? null : d.milestone.get(d.plan.get(task.plan_id).milestone_id));
  // The priority pm queues the task by (effective_priority), and where it comes from.
  d.prio = (task) => ({ value: task.effective_priority ?? 0,
    from: task.priority != null ? "task" : task.plan_id != null && d.plan.get(task.plan_id).priority != null ? "plan" : "default" });
  // Pending decisions an open task of milestone m waits for: the ones the milestone needs answered to finish.
  d.milestoneDecisions = (m) => d.decisions.filter((x) => x.status === "pending"
    && (d.tasksWaiting.get(x.id) || []).some((t) => t.status === "open" && d.milestoneOfTask(t)?.id === m.id));
  d.blockers = (task) => (d.decisionsOf.get(task.id) || []).filter((x) => x.status === "pending");
  d.excluded = (task) => {
    if (task.status !== "open") return null;
    const m = d.milestoneOfTask(task);
    if (m && m.status !== "open") return `milestone ${m.id} is ${m.status.replace("-", " ")}`;
    const b = d.blockers(task);
    if (b.length) return b;
    const u = d.unmet(task);
    return u.length ? `waits for ${u.map((x) => `task ${x.id}${x.status === "dropped" ? " (dropped)" : ""}`).join(", ")}` : null;
  };
  // Pending decisions by what answering them frees: alone = open tasks it is the only pending blocker of; waiting = all
  // open tasks it blocks. Most freed first.
  d.blocking = () => d.decisions.filter((x) => x.status === "pending").map((x) => {
    const waiting = (d.tasksWaiting.get(x.id) || []).filter((t) => t.status === "open");
    return { x, waiting, alone: waiting.filter((t) => d.blockers(t).length === 1) };
  }).filter((b) => b.waiting.length)
    .sort((a, b) => b.alone.length - a.alone.length || b.waiting.length - a.waiting.length || a.x.id - b.x.id);
  d.held = (m) => !!m && m.status === "on-hold";
  d.taskState = (task) => (task.status === "open" && d.held(d.milestoneOfTask(task)) ? "on-hold" : task.status);
  d.cost = (runs) => runs.reduce((s, r) => s + (r.cost_usd || 0), 0);
  d.tokens = (r) => (r.input_tokens || 0) + (r.output_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_creation_tokens || 0);
  d.taskRuns = (task) => d.runsOf.get(task.id) || [];
  // Warm hand-off: the runs of one Claude session, oldest first; [r] if r is in none.
  const next = new Map(t.run.filter((r) => r.continues_run != null).map((r) => [r.continues_run, r]));
  d.chain = (r) => {
    const c = [r];
    for (let p = r; p.continues_run != null && (p = d.run.get(p.continues_run)); ) c.unshift(p);
    for (let n = next.get(r.id); n; n = next.get(n.id)) c.push(n);
    return c;
  };
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
