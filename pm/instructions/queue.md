## What pm queues

pm adds these tasks itself; their bodies are the prompts of the same name in project-manager.toml. A queued one that isn't running yet gets the new text instead of a second task (a running one may have read the old text).

| Trigger | Task (kind) | Plan |
|---|---|---|
| `pm milestone add` | "write the masterplan of milestone N: …" (`masterplan`, priority 1000) | masterplan, body "tbd." |
| plan added, or its subject or body changed | "create tasks for plan N: …" (`split`, priority 1000) | that plan |
| milestone's subject or body changed, no masterplan task waiting | "revise the masterplan of milestone N: …" (`revise`) | masterplan |
| plan's subject or body changed | "revise plan M: …" (`revise`), parent quoted | each child plan that isn't done |
| a milestone's last open task closed | "post-milestone cleanup of milestone N: …" (`cleanup`); closed too: milestone implemented | masterplan |
| a task failed | "investigate the failure of task N: …" (`investigate`), above the failed task | the failed task's |
| `pm summary request` | "write an executive summary" (`exec`, priority 10000) | none |
| `pm report request` | "write a resource use report" (`exec`, priority 10000) | none |
| `pm report ask "<question>"` | "answer: <question>" (`exec`, priority 10000), a custom report | none |

A priority change queues nothing. When done, pm checks: `plan` and `masterplan` tasks added or changed a plan (`revise` may leave it as it is), `split` left its plan with tasks; otherwise the task fails.

`pm queue` shows a priority only when it isn't 0:

```
Queue (3):
   +1000  task 1: write the masterplan of milestone 1: CSV export
   +1000  task 3: create tasks for plan 2: Backend endpoint
    +700  task 4: stream one table as CSV
```

## Priorities

- A hint, not a guarantee: higher runs first, within the same milestone queue index (a lower queue index runs before any priority) and the same work tree; for a strict order, `--requires`
- A task without its own priority gets its plan's
- Planning (`plan`, `masterplan`, `split`): `planning_priority`, 1000, unless given
- Rules for choosing them: `plan_priority_prompt` and `task_priority_prompt` in project-manager.toml, appended to planning workers' prompts and printed below (`pm instructions queue`)
