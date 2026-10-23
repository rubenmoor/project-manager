## Decisions

### Ask the human

```sh
did=$(pm decision add "Which CSV delimiter?" \
        --intro "Exported files can separate their values with semicolons or with commas. Semicolons open directly in German Excel; commas are the international standard but need an import step there." \
        --body "German Excel opens ';' files directly, ',' needs an import step." \
        --option "semicolon" --option "comma")
pm task-pending-decision 5 "$did"       # task 5 waits until the human answers
```

Put the recommended option first. `decision add` links no task: run `pm task-pending-decision` for every task that waits for the answer, your own included. `--once`: a one-off go-ahead or check, not a lasting rule.

### Iterative feedback

When a task or its plan asks for iterative feedback, or the work needs the human's judgement of a result rather than a choice between options: work up to the point where you need the human, ask (`pm decision add`: what to look at and judge; options only if there are real alternatives), link your task, stop. The answer brings the task back with the human's input; repeat until it's good, then finish. Splitting a plan that asks for it: say "iterative feedback" in the bodies of the tasks it concerns.

### The decision log

The answered `rule` decisions that nothing fully supersedes. Every task, planning ones included, starts here: grep it for what the task touches (subjects, files, terms; `--grep` repeats, any word matches), and again before every choice the plan doesn't settle. Answered decisions are binding until a newer one supersedes them: a plan follows them, a task implements them, neither asks again. Cite them as `decision 32: <subject>`, in code, prompts and docs too.

```sh
pm decision list --answered --grep csv --grep delimiter   # the log, newest first
pm decision list --all --grep delimiter                   # pending, one-off, superseded, irrelevant too
pm decision show 32                                       # = pm show d32
```

**Your own choice:** a lasting choice the plan doesn't make (a design, a format, a rule others must follow) that the human needn't make first: record it, answered. The human overrides it by superseding it. A question only the human can answer stays a question (`pm decision add`). You may supersede only a worker's choice; to change a human's answer, ask with `pm decision add` and name the decision.

```sh
pm decision record "Number style in prompts" --answer "RAE: 1 234,5; scan flags dotted thousands" \
    --intro "Numbers in the questions are now written the Spanish way, with a space between thousands and a decimal comma, because that is how the source data writes them." \
    --body "Prompts mixed three styles; RAE is what the data uses."
pm decision record "Seen counters" --answer "…" --supersedes 41   # replaces decision 41, a worker's own choice
pm decision supersede 44 28 --note "scope only"                  # decision 44 replaces only the scope of 28
```

**Consequences:** when `pm show` says your task is the last open one that waited for a decision, record what changed because of the answer, before `pm task done`:

```sh
pm decision consequences 32 "RAE number style in all prompts (prompts/*.md); scan.py flags dotted thousands"
```
