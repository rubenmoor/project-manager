For planning:
  pm plan list     plans with open tasks, their ids and priorities: check before adding one
  pm plan add <milestone id> "<subject>" --intro "<intro>" --priority N < body.md     queues a task that splits it into tasks
  pm plan edit <plan id> [--subject …] [--body -] [--intro …] [--priority N]     a new subject or body queues its split and the revision of the plans below it
  pm milestone list; pm milestone show <milestone id>
  pm task add … [--kind plan] [--model opus] [--web] [--priority N]     --kind plan: the task writes a plan; --model opus: a work task that needs deep reasoning (subtle design, hard bugs, wide refactors), else pm picks by kind; --web: its worker gets WebSearch and WebFetch (after the human's sign-off)
  pm task set <task id> [--subject …] [--body -] [--priority N|none] [--web|--no-web] [--model opus|none] [--worktree <name>]     an open task that isn't running
  pm task drop <task id> "<reason>" [--replaced-by <task id>]     the new task takes over its prerequisite links
A task fits one worker run: one change you can describe, check and commit. Splitting a plan: keep its done and dropped tasks, drop open ones that no longer fit, add the missing ones in the order they should run, `--requires` where one needs another's result (priority is only a hint, and orders nothing across work trees); the masterplan's own tasks write plans (`--kind plan`). Raise a decision for each question only the human can answer and link the tasks that wait for it. A plan that asks for iterative feedback: say "iterative feedback" in the bodies of the tasks it concerns. Note on `pm task done` after a split: "<date>, tasks …".

{priorities}
