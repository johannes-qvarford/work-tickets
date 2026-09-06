# Orchestrator

Load [`../SKILL.md`](../SKILL.md) first, then this file. This file is for the orchestrator only. It owns issue selection, state inspection, delegation, sequencing, loop termination, and final reporting. Do not perform implementation, verification, or Pull Request lifecycle work in parallel.

## Selection and State Inspection

Run the commands below in order, substituting the issue number and branch slug where shown. The fenced snippets are stages of one shell session so variables set earlier remain available. If a later snippet is copied independently, rerun its setup commands and honor its `: "${VAR:?}"` guards; never let an unset variable become an empty `gh` or `git` argument. A command that fails is a blocker; do not continue by guessing or by hiding the failure with `|| true`.

The workflow has two required `main` synchronization boundaries. Before selecting or working on any issue, establish the latest clean `main` baseline from `origin`. After the issue/Pull Request lifecycle has finished, synchronize `main` again before reporting termination. In this repository, `DEFAULT_BRANCH` must resolve to `main`; verify that value through `gh repo view` before proceeding.

```sh
set -eu
gh auth status
gh repo view --json nameWithOwner,defaultBranchRef
REPO="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')"
: "${REPO:?Repository lookup returned no repository}"
: "${DEFAULT_BRANCH:?Repository lookup returned no default branch}"
test "$DEFAULT_BRANCH" = main || {
  printf '%s\n' "Expected the repository default branch to be main, got $DEFAULT_BRANCH" >&2
  exit 1
}
git status --short --branch
test -z "$(git status --porcelain)" || {
  printf '%s\n' 'Working tree is not clean; preserve those changes and stop before branching.' >&2
  exit 1
}
git fetch origin main
git switch main
git pull --ff-only origin main
gh issue list --repo "$REPO" --state open --limit 100 \
  --json number,title,url,labels,assignees,state,updatedAt
```

The initial synchronization must complete before issue selection, branching, or any implementation work. The clean-tree check is a safety gate: preserve any existing uncommitted or unrelated changes and stop rather than switching branches or trying to make the tree appear clean. Both the initial and final synchronization must remain fast-forward-only; never reset, clean, rebase, force-push, or otherwise discard work to make `main` match `origin`.

Select exactly one eligible issue in the requested or selected order. Exclude Pull Requests, closed issues, explicitly blocked issues, and issues already covered by a merged Pull Request. An open Pull Request is resumed after inspection. A closed and unmerged Pull Request is not replaced automatically. If the user names an issue, verify that it exists and is actionable. If appropriate and permitted, assignment is only a coordination aid, not proof of completion.

For the selected issue, inspect its complete context before changing files:

```sh
ISSUE=123
: "${REPO:?Set REPO before inspecting an issue}"
: "${ISSUE:?Set ISSUE to the selected issue number}"
gh issue view "$ISSUE" --repo "$REPO" --comments \
  --json number,title,url,body,state,labels,assignees,comments
```

Before creating or resuming a branch, search for an existing issue branch and Pull Request. Classify every clearly matching Pull Request before continuing:

- An open Pull Request is resumed after the read-only inspection below; do not create a duplicate.
- A merged Pull Request must not get a duplicate. Verify the issue state, treat the issue as already covered, and report that outcome.
- A closed and unmerged Pull Request is not replaced automatically. Stop and report the exact Pull Request and the intervention needed.
- If no matching Pull Request exists, proceed normally with the issue branch.

```sh
: "${REPO:?Set REPO before searching for existing work}"
: "${DEFAULT_BRANCH:?Set DEFAULT_BRANCH before searching for existing work}"
: "${ISSUE:?Set ISSUE before searching for existing work}"
BRANCH="issue/$ISSUE-short-slug"
: "${BRANCH:?Set BRANCH before searching for existing work}"
gh pr list --repo "$REPO" --state all --head "$BRANCH" \
  --json number,title,url,state,isDraft,headRefName,baseRefName
git branch --list "$BRANCH"
```

For an open Pull Request, set `PR` to the matching number and perform this read-only inspection before continuing. It records Pull Request metadata, commits, files, comments, checks, merge state, branch history, and the complete branch diff against the default branch. Comments are part of the required existing-Pull-Request review before resuming work:

```sh
: "${REPO:?Set REPO before inspecting a Pull Request}"
: "${DEFAULT_BRANCH:?Set DEFAULT_BRANCH before inspecting a Pull Request}"
: "${BRANCH:?Set BRANCH before inspecting a Pull Request}"
PR=123
: "${PR:?Set PR to the matching open Pull Request number}"
gh pr view "$PR" --repo "$REPO" \
  --json number,title,url,state,isDraft,baseRefName,headRefName,mergedAt,closedAt,mergeCommit,mergeStateStatus,reviewDecision,statusCheckRollup,commits,files,comments,closingIssuesReferences
git log --oneline --decorate --graph "$BRANCH"
git diff --stat "$DEFAULT_BRANCH...$BRANCH"
git diff "$DEFAULT_BRANCH...$BRANCH"
```

For a merged Pull Request, verify the issue state before treating it as already covered:

```sh
: "${REPO:?Set REPO before verifying a merged Pull Request}"
: "${ISSUE:?Set ISSUE before verifying a merged Pull Request}"
PR=123
: "${PR:?Set PR to the matching merged Pull Request number}"
gh issue view "$ISSUE" --repo "$REPO" --json number,title,state,closedAt
```

For a new branch when no matching Pull Request exists, run `git switch -c "$BRANCH" "$DEFAULT_BRANCH"`. For a resumed open branch, inspect it and its Pull Request first, then run `git switch "$BRANCH"`; never silently discard its commits or working-tree changes. A closed and unmerged Pull Request stops this workflow rather than proceeding to branch creation or Pull Request creation.

## Delegation Sequence

Repeat these stages for one issue until it is merged or an explicit blocker requires user intervention:

1. Prepare an issue branch named with the issue number and a short sanitized slug, based on the current default branch and containing no unrelated changes. Never implement directly on the default branch.
2. Delegate implementation to one subagent using the template in [`implementor.md`](implementor.md). Include the exact issue number, title, URL, body, acceptance criteria, relevant comments, repository instructions, branch, and issue-only constraints. Review its report and workspace diff. If incomplete, send precise deficiencies to a new implementation cycle.
3. Delegate local verification to a separate subagent using [`verifier.md`](verifier.md). If it returns `FAIL`, use the findings in a new implementation cycle and verify again. Do not create or merge a Pull Request while required local verification is failing.
4. After verification passes, delegate the Pull Request lifecycle to [`pull-request.md`](pull-request.md). Keep only one issue implementation and one Pull Request lifecycle active at a time.
5. Review reports and current state at every handoff. If Pull Request validation fails because code or tests need changes, start a new implementation cycle on the existing issue branch and repeat local verification and Pull Request validation. If it fails because of a transient CI or access problem, wait or report the blocker; never declare success.
6. Preserve the issue number, title, acceptance criteria, labels, and relevant comments accurately.

## Loop Termination and Reporting

List eligible open issues at the beginning and after every completed issue with structured `gh issue list --json` output where possible. Continue until no eligible issues remain or the user-supplied limit or boundary is reached. Do not silently skip an issue; report ambiguity, blocking, existing work, or unsuitability and ask the user unless a safe repository rule resolves it.

When all issue and Pull Request lifecycle work is finished, perform the final `main` synchronization below before reporting termination. This is required both after the last completed issue and when the loop stops because there are no eligible issues or the requested limit/boundary has been reached. If a lifecycle blocker leaves work in progress, do not bypass it just to synchronize; report the blocker and leave the worktree untouched unless it is independently safe to run this stage.

```sh
: "${DEFAULT_BRANCH:?Set DEFAULT_BRANCH before final synchronization}"
test "$DEFAULT_BRANCH" = main || {
  printf '%s\n' "Expected the repository default branch to be main, got $DEFAULT_BRANCH" >&2
  exit 1
}
git status --short --branch
test -z "$(git status --porcelain)" || {
  printf '%s\n' 'Working tree is not clean; preserve those changes and stop before final synchronization.' >&2
  exit 1
}
git fetch origin main
git switch main
git pull --ff-only origin main
```

The final synchronization must not discard uncommitted or unrelated changes. If the clean-tree check fails, leave the current branch and changes exactly as they are and report that `main` could not be synchronized. If `git switch` or `git pull --ff-only` fails, stop and report the failure; do not use a non-fast-forward update or a destructive command as a workaround.

At the end, report:

- Each completed issue number and title.
- The issue URL and dedicated Pull Request URL.
- The branch, implementation commit SHA, merge commit SHA when available, and merge method.
- Local checks, GitHub check runs, and their final statuses.
- Any issues skipped, blocked, already in progress, or left open, with the exact reason.
- The next eligible issue when stopping at a user-supplied limit.

Do not report an issue as completed based solely on a created Pull Request, a pushed branch, a passing local test, or an attempted merge.
