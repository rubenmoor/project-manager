## Recipes

Each pm task's body says what to do; these are the mechanics.

- **Write the masterplan** (plan P): read the milestone (`pm show <task>`) and the code; `pm plan edit P --intro "…" --body -`, divided into parts, stating that the masterplan's own tasks each write the plan for one part. That queues "create tasks for plan P: …".
- **Revise a plan** (P): if it no longer fits its milestone or parent plan, `pm plan edit P --body -`; that queues its split and the revisions below it. If it still fits, change nothing.
- **Split a plan into tasks** (P): keep the done and dropped tasks, drop open ones that no longer fit, add one `pm task add P … --worktree <name>` per missing task, in the order they should run, `--requires <task id>` where one needs another's result (priority is a hint and orders only within a work tree) (`--model opus` for one that needs deep reasoning); the masterplan's tasks write plans (`--kind plan`). Raise a decision for each question only the human can answer, and link the tasks that wait for it. Note: `"<date>, tasks …"`.
- **Write a part's plan** (a task of the masterplan): `pm plan list` first, then `pm plan add <milestone> "<part>" --priority N --intro "…"` with the full body.
- **Post-milestone cleanup:** work through the body for the whole project. For another milestone that needs a change, or a pending decision that seems obsolete: a new decision for the human.
- **Executive summary** (`exec`): `pm summary show` is the previous one; store the new one with `pm summary add < summary.md` (only in an exec task).
- **Resource use report** (`exec`): `pm report tasks` lists the done tasks; store with `pm report add < report.json`, `{"categories": [{"name": "…", "criteria": "…", "tasks": [3, 7]}]}`; refused unless every done task is in exactly one category. pm sums each category's time, tokens and cost.
- **Custom report** (`exec`): the owner's question or request, in the task body; answer in one plain paragraph, store with `pm report answer < answer.md`. `pm show c2`.
- **Your task needs an answer first:** `pm decision add`, `pm task-pending-decision <your task> <decision>`, stage what's done, stop. `pm run` goes on with the next task.
- **Your task needs another task's result first:** `pm task requires <your task> <task id>` (add that task first if there is none), stage what's done, stop. It runs again once that task is done.
- **Finish:** run the project's checks, stage the changes meant to stay, delete temporary files, then `pm task done <task> "<YYYY-MM-DD, files>"` or `pm task drop <task> "<reason>"`. A worker run ends with its task closed or waiting for a decision or a prerequisite task (web: `pm instructions tasks`); otherwise pm keeps it going once.
