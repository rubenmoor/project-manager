## For the human

Agents run these only for the human, in the human's own session.

```sh
pm init              # once per project: project-manager.toml (+ AGENTS.md, CLAUDE.md if neither exists), committed
pm run               # work the queue until it is empty, Claude needs the human or the tree is dirty
pm run --once        # one task;  --dry-run: print the next worker command
pm stop              # the running `pm run` stops after its current task
pm kill 7            # kill running task 7 now: run killed, task dropped, no investigation, leftovers stashed
pm task redo 7       # a dropped task again: a new open task like it, with its stashed leftovers
pm decide            # answer pending decisions interactively (don't run it as an agent)
pm decision list                       # pending decisions: context, options, blocked tasks
pm decision answer 3 1                 # option 1; or own text: pm decision answer 3 "tabs"
pm decision answer 3 1 --once --supersedes 2
pm decision irrelevant 3 "<reason>"    # drops the open tasks it blocks
pm decision approve 4                  # sign-offs only
pm decision kind 3 rule                # rule: lasting, in the log; once: a one-off
pm task add --exec "<subject>" [--priority N] < body.md
pm summary request ; pm report request # queue an executive summary / resource use report; show, list
pm report ask "<question>"             # queue a custom one-paragraph report
```

Settings in `project-manager.toml`: `[models]` (the worker's model per task kind, `escalate` after a failure), `push_minutes` (`pm run` pushes the trunk's branch at most this often, default 60, 0: off).

Record only answers the human gave.
