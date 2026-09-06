---
name: do-github-issues
description: "Work through GitHub issues one at a time using an orchestrated implementation, verification, Pull Request, CI validation, and merge workflow. Use when the user asks to execute GitHub issues, process open issues, or implement an issue end to end."
argument-hint: "Optional: specify an issue number, label/filter, starting issue, or maximum number of issues"
user-invocable: true
disable-model-invocation: false
---

# Do GitHub Issues

## Purpose

Process GitHub issues sequentially. The orchestrator owns the loop and delegates implementation, verification, and Pull Request lifecycle work to subagents. Each issue is an independent unit of work: it gets its own branch and Pull Request, and the Pull Request must be validated and merged before the issue is considered complete. Never combine multiple issues into one implementation or Pull Request.

Load this entrypoint for every workflow. Load exactly one role file in addition to it:

- The orchestrator loads [`assets/orchestrator.md`](assets/orchestrator.md) for issue selection, state inspection, delegation, sequencing, loop termination, and reporting.
- An implementation subagent loads [`assets/implementor.md`](assets/implementor.md) for implementation constraints and reporting.
- A verification subagent loads [`assets/verifier.md`](assets/verifier.md) for local review, checks, and pass/fail reporting.
- A Pull Request lifecycle subagent loads [`assets/pull-request.md`](assets/pull-request.md) for staging, commit, push, Pull Request validation, merge, and post-merge confirmation.

Role files are not standalone: apply the shared rules in this file first.

## Required Tools and Preconditions

- Use the `gh` CLI for GitHub operations. Do not infer repository, issue, Pull Request, check, or merge state from local assumptions.
- Confirm `gh auth status` and `gh repo view` before selecting an issue. If authentication or repository access is unavailable, stop and report the blocker.
- Read `AGENTS.md`, contribution instructions, and relevant project documentation before implementation.
- Confirm the repository's default branch and current working tree before creating a branch. Preserve unrelated user changes and never commit them.

## Workflow

The orchestrator selects one eligible issue, prepares or resumes its issue branch, delegates implementation, obtains an independent local verification pass, and hands the verified work to the Pull Request lifecycle subagent. That subagent commits, pushes, creates or resumes exactly one Pull Request, validates GitHub checks and mergeability, and confirms the merge and linked issue state. The orchestrator repeats this sequence until the requested limit is reached or an explicit blocker requires intervention.

The role files contain the complete stage instructions, including the detailed command runbook from issue #11. Do not skip the role-specific instructions because local success alone is not completion.

When a role file provides command snippets, run them in order as stages of one shell session so variables set earlier remain available. If a later snippet is copied independently, rerun its setup commands and honor every `: "${VAR:?}"` guard; never let an unset variable become an empty `gh` or `git` argument. A command that fails is a blocker; do not continue by guessing or by hiding the failure with `|| true`.

## Shared Safety and Recovery

- Preserve unrelated working-tree and staging-area changes; keep them out of issue branches and commits.
- Never expose credentials, tokens, or secret values.
- Never use destructive Git commands, force-push, administrator merge, merge bypasses, or skipped hooks/checks without explicit user authorization for that exact action.
- Never open duplicate Pull Requests for the same issue; search existing branches and Pull Requests first.
- Never merge a Pull Request with failing, pending, skipped-required, unavailable, or unknown required checks.
- Never close an issue manually solely because a Pull Request was created or because merge automation did not run yet.
- Do not claim an issue is complete until its implementation has been verified, its dedicated Pull Request has passed validation, and the Pull Request has actually merged.
- Respect repository branch protection and required-review policies. If the authenticated actor cannot satisfy them, stop rather than bypassing them.
- If a subagent reports ambiguity, inspect the repository and issue context, then ask the user only for information that cannot be determined safely.
- If a Pull Request cannot be merged because of access, policy, or required human review, leave it open and report the exact intervention required.
