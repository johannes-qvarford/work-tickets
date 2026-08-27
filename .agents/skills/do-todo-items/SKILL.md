---
name: do-todo-items
description: "Work through TODO.md one item at a time using an orchestrated implementation, verification, commit, push, and GitHub pipeline workflow. Use when the user asks to execute TODO items, complete the project TODO, or iteratively implement outstanding checklist items."
argument-hint: "Optional: specify a TODO item, starting item, or maximum number of items"
user-invocable: true
disable-model-invocation: false
---

# Do TODO Items

## Purpose

Process the repository's `TODO.md` sequentially. The orchestrator owns the loop and delegates implementation, verification, and Git/GitHub operations to subagents. Never treat multiple TODO entries as one task.

## Orchestration Rules

- Read `TODO.md` at the beginning and after every completed item.
- Select exactly one unchecked TODO item at a time, in top-to-bottom order, unless the user explicitly names a different item.
- Keep only one implementation/verification cycle active at a time.
- The orchestrator must preserve the TODO item's exact wording when reporting or updating it.
- Do not mark an item complete until implementation, verification, and the corresponding commit have succeeded.
- Do not claim success for a GitHub operation that was not actually completed or whose status is unknown.
- Respect repository instructions such as `AGENTS.md`, contribution rules, required checks, and protected-branch policies.

## Per-Item Procedure

For each unchecked item, repeat these stages until the item is fully done:

### 1. Delegate implementation

Start one subagent with a focused prompt that includes:

- The exact TODO item.
- Relevant repository instructions and constraints.
- The expectation to inspect existing code and tests before editing.
- The expectation to implement only this item, add or update tests as appropriate, and report changed files and checks run.
- A requirement not to commit or push; Git operations belong to the commit subagent.

The orchestrator reviews the implementation report and workspace changes before continuing. If the subagent cannot complete the item, use its report to refine the next implementation prompt.

### 2. Delegate verification

After implementation, start a separate verification subagent. It must:

- Review the diff against the exact TODO item and repository instructions.
- Run the relevant focused tests/checks, then the complete checks required by the repository when practical.
- Inspect for regressions, missing tests, formatting/type/lint errors, and unrelated changes.
- Make no code changes and do not commit or push.
- Return a clear pass/fail report with actionable failure details.

If verification fails, send the reported issues to a new implementation subagent. Then run a new verification subagent. Repeat until verification passes. Do not proceed to Git operations while verification is failing.

### 3. Delegate commit, push, and pipeline monitoring

When verification passes, start a commit subagent with:

- The exact TODO item and a concise summary of the implementation.
- The verified checks and their results.
- Instructions to inspect the diff, stage only files belonging to this item, create an appropriately scoped commit, and push the current branch according to repository policy.
- Instructions to wait for the GitHub Actions/CI pipeline for that pushed commit and report the exact run and final status.
- A prohibition on rewriting unrelated history or committing unrelated changes.

If commit, push, or pipeline monitoring fails, do not mark the TODO item complete. Start an implementation subagent to address the failure when code/config changes are needed, then start a verification subagent again. If the failure is purely environmental or access-related, report the blocker clearly and ask the user for the required intervention rather than falsely completing the item.

### 4. Complete the item

Only after the commit subagent reports a successful commit, push, and passing GitHub pipeline:

1. Confirm the repository state and commit result.
2. Update the matching unchecked checkbox in `TODO.md` to checked, preserving the item's text and ordering.
3. Commit the TODO checkbox update as the TODO item's completion. Prefer delegating this to a separate commit subagent, which must stage only the checklist update, create a concise completion commit, push it, and wait for its pipeline status.
4. If this completion commit or its pipeline fails, keep the item unchecked and resume implementation/verification or request user intervention as appropriate.
5. Re-read `TODO.md` before selecting the next item.

The implementation commit and the TODO-completion commit are distinct commits unless repository policy explicitly requires another approach.

## Loop Termination

Continue the same per-item procedure until no unchecked items remain. At the end, report:

- Each completed TODO item.
- The implementation and completion commit identifiers.
- Verification checks and GitHub pipeline results.
- Any unresolved blockers; never silently skip an item.

If the user supplied a starting item or limit, stop only at that requested boundary and state what remains.

## Subagent Prompt Templates

Use these templates as a baseline and fill in repository-specific details.

### Implementation subagent

> Implement exactly this TODO item: **[item text]**. First inspect `AGENTS.md`, the relevant source, tests, and current diff. Make the smallest complete change, add/update tests, and run relevant checks. Do not commit or push. Report files changed, behavior implemented, checks run, and any blockers.

### Verification subagent

> Verify exactly this TODO item: **[item text]** after the current implementation. Do not edit, commit, or push. Review the diff and run focused and repository-required checks. Look for regressions, missing coverage, unrelated changes, and violations of `AGENTS.md`. Return PASS only when the item is complete; otherwise return FAIL with precise remediation steps.

### Commit/pipeline subagent

> For TODO item **[item text]**, inspect the verified diff, stage only related files, commit with a concise message, push according to repository policy, and wait for the GitHub pipeline for the pushed commit. Report commit SHA, pushed branch, pipeline URL/run, and final status. Do not include unrelated changes.

## Safety and Recovery

- Never use destructive Git commands, force-push, or bypass hooks/checks unless the user explicitly authorizes it.
- Never expose credentials, tokens, or secret values in reports.
- If the working tree contains unrelated user changes, preserve them and keep them out of commits.
- If a subagent reports ambiguity, inspect the repository and ask the user only for information that cannot be determined safely.
- A failed pipeline is a failure of the item, even if local checks pass; resume the implementation/verification loop after fixing the cause.