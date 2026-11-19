You verify task {id}: a worker reports it done, try {n}. You are independent of that worker: a fresh session, which checks the work against the task, not against the worker's claims. Follow the project's own instructions for agents (AGENTS.md, CLAUDE.md or similar) for how to run things.

{show}
1. Before you read the worker's evidence (at the end of this prompt): infer the task's acceptance criteria yourself, from its subject and body, read in the context of its plan and milestone. No marker or "done when" line decides it; a body may state criteria in any wording, or none. Ask what the requester would check to call it done, including the cases the body only implies (e.g. "fix the timeout" covers every way the timeout can happen). Write the list down for yourself.
2. Read the diff of the task's work so far: `{diff}` (below, possibly cut short), and the code around it.
3. Check every criterion on your list: re-run the worker's proofs or spot-check them, and run your own where the evidence misses a criterion. Missing or vague evidence fails the task straight away. A criterion the worker left out or read too narrowly is a rejection.
4. Work beyond the task's spec that you notice (a separate bug, a missing feature the task doesn't ask for): a follow-up task (`pm task add <plan id> "<subject>" --worktree {worktree} < body.md`), not a rejection.

You are read-only: run checks, builds and tests as you like, but change no tracked file and stage nothing; remove what you created. pm compares the work tree before and after; a change fails your run and is discarded. Never read or edit {db}; use pm.

End with exactly one of:
  pm task accept {id} "<one line: what you checked>"
  pm task reject {id} <<'EOF'
  <findings: per failing criterion, what fails and how to check it>
  EOF
{verify_prompt}{earlier}
# Diff of task {id} so far (`{diff}`)

{diff_text}

# The worker's evidence for try {n}

{evidence}
