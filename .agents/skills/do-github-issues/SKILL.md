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

## Required Tools and Preconditions

- Use the `gh` CLI for GitHub operations. Do not infer repository, issue, Pull Request, check, or merge state from local assumptions.
- Confirm `gh auth status` and `gh repo view` before selecting an issue. If authentication or repository access is unavailable, stop and report the blocker.
- Read `AGENTS.md`, contribution instructions, and relevant project documentation before implementation.
- Confirm the repository's default branch and current working tree before creating a branch. Preserve unrelated user changes and never commit them.
- Do not expose credentials, tokens, or secret values in prompts, logs, or reports.
- Never use force-push, destructive Git commands, merge bypasses, or skipped hooks/checks unless the user explicitly authorizes the specific action.

## Command Runbook

Run the commands below in order, substituting the issue number and branch slug where shown. The fenced snippets are stages of one shell session so variables set earlier remain available. If a later snippet is copied independently, rerun its setup commands and honor its `: "${VAR:?}"` guards; never let an unset variable become an empty `gh` or `git` argument. A command that fails is a blocker; do not continue by guessing or by hiding the failure with `|| true`.

```sh
set -eu
gh auth status
gh repo view --json nameWithOwner,defaultBranchRef
REPO="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')"
: "${REPO:?Repository lookup returned no repository}"
: "${DEFAULT_BRANCH:?Repository lookup returned no default branch}"
git status --short --branch
test -z "$(git status --porcelain)" || {
  printf '%s\n' 'Working tree is not clean; preserve those changes and stop before branching.' >&2
  exit 1
}
git fetch origin "$DEFAULT_BRANCH"
git switch "$DEFAULT_BRANCH"
git pull --ff-only origin "$DEFAULT_BRANCH"
gh issue list --repo "$REPO" --state open --limit 100 \
  --json number,title,url,labels,assignees,state,updatedAt
```

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

For an open Pull Request, set `PR` to the matching number and perform this read-only inspection before continuing. It records the Pull Request metadata, the branch history, and the complete branch diff against the default branch:

```sh
: "${REPO:?Set REPO before inspecting a Pull Request}"
: "${DEFAULT_BRANCH:?Set DEFAULT_BRANCH before inspecting a Pull Request}"
: "${BRANCH:?Set BRANCH before inspecting a Pull Request}"
PR=123
: "${PR:?Set PR to the matching open Pull Request number}"
gh pr view "$PR" --repo "$REPO" \
  --json number,title,url,state,isDraft,baseRefName,headRefName,mergedAt,closedAt,mergeCommit,mergeStateStatus,reviewDecision,statusCheckRollup,commits,files,closingIssuesReferences
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
gh issue view "$ISSUE" --repo "$REPO" --json number,title,url,state,closedAt
```

For a new branch when no matching Pull Request exists, run `git switch -c "$BRANCH" "$DEFAULT_BRANCH"`. For a resumed open branch, inspect it and its Pull Request first, then run `git switch "$BRANCH"`; never silently discard its commits or working-tree changes. A closed and unmerged Pull Request stops this workflow rather than proceeding to branch creation or Pull Request creation.

After local verification passes, inspect the final diff, stage only issue files, and create or resume exactly one Pull Request:

```sh
: "${REPO:?Set REPO before staging and creating a Pull Request}"
: "${DEFAULT_BRANCH:?Set DEFAULT_BRANCH before staging and creating a Pull Request}"
: "${ISSUE:?Set ISSUE before staging and creating a Pull Request}"
: "${BRANCH:?Set BRANCH before staging and creating a Pull Request}"
git status --short --branch
git diff --check
git diff --name-only
git add -- path/to/issue-file path/to/test-file
git diff --cached --check
git diff --cached --stat
git commit -m "Implement issue #$ISSUE"
git push --set-upstream origin "$BRANCH"
PR_BODY="$(printf '%s\n' \
  '## Summary' \
  '- [Replace with a concise summary of the implemented change.]' \
  '' \
  '## Validation' \
  '- [Replace with the local checks run and their results.]' \
  '' \
  "Fixes #$ISSUE")"
: "${PR_BODY:?Set PR_BODY before creating a Pull Request}"
gh pr create --repo "$REPO" --base "$DEFAULT_BRANCH" --head "$BRANCH" \
  --title "[#${ISSUE}] Short issue title" --body "$PR_BODY"
```

Replace the `git add` paths, PR title, and the two PR body template lines with the actual issue details before running the commands. The body visibly includes a change summary, local validation, and `Fixes #$ISSUE` when appropriate. If the branch already has a matching open PR, do not run `gh pr create`; use its number instead. A merged or closed/unmerged matching PR follows the lifecycle rules above and never gets a duplicate.

For PR number `PR`, inspect metadata and the exact GitHub diff before waiting for checks:

```sh
: "${REPO:?Set REPO before inspecting a Pull Request}"
PR=123
: "${PR:?Set PR before inspecting a Pull Request}"
gh pr view "$PR" --repo "$REPO" \
  --json number,title,url,state,isDraft,baseRefName,headRefName,mergeStateStatus,reviewDecision,statusCheckRollup,commits,files,closingIssuesReferences
gh pr diff "$PR" --repo "$REPO"
gh pr checks "$PR" --repo "$REPO" --watch
```

Treat a failed, cancelled, skipped-required, pending, unavailable, or unknown required check as a failure. Re-run `gh pr view` after checks finish to confirm the base/head, merge state, reviews, checks, and issue linkage still match the selected work.

Only after all PR validation passes, use the permitted merge command and then confirm both PR and issue state:

```sh
: "${REPO:?Set REPO before merging a Pull Request}"
: "${DEFAULT_BRANCH:?Set DEFAULT_BRANCH before merging a Pull Request}"
: "${ISSUE:?Set ISSUE before merging a Pull Request}"
PR=123
: "${PR:?Set PR before merging a Pull Request}"
gh pr merge "$PR" --repo "$REPO" --squash --delete-branch
gh pr view "$PR" --repo "$REPO" \
  --json number,state,mergedAt,mergeCommit,baseRefName,headRefName,mergeStateStatus
gh issue view "$ISSUE" --repo "$REPO" --json number,state,closedAt
MERGE_SHA="$(gh pr view "$PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')"
git fetch origin "$DEFAULT_BRANCH"
git merge-base --is-ancestor "$MERGE_SHA" "origin/$DEFAULT_BRANCH"
git log -1 --oneline "origin/$DEFAULT_BRANCH"
gh issue list --repo "$REPO" --state open --limit 100 \
  --json number,title,url,labels,assignees,state,updatedAt
```

Do not use `--admin`, force-push, or a merge-bypass option. If merge or confirmation fails, leave the PR open and report the exact command failure.

## Orchestration Rules

- List eligible open issues at the beginning and after every completed issue. Use structured `gh issue list --json` output where possible.
- Select exactly one issue at a time, in the requested or selected order. If the user names an issue, verify that it exists and is actionable before starting it.
- Keep only one issue implementation and one Pull Request lifecycle active at a time.
- Preserve the issue number, title, acceptance criteria, labels, and relevant comments accurately in subagent prompts and reports.
- Do not silently skip an issue. If an issue is ambiguous, blocked, already being worked on, or unsuitable for automatic implementation, report the reason and ask the user unless a safe repository rule resolves it.
- Do not claim an issue is complete until its implementation has been verified, its dedicated Pull Request has passed validation, and the Pull Request has actually merged.
- A successful local test run is not enough. The Pull Request's required checks and mergeability must be inspected with `gh` before merging.
- Respect repository branch protection and required-review policies. If the authenticated actor cannot satisfy them, stop rather than bypassing them.

## Issue Selection

1. Determine the repository with `gh repo view --json nameWithOwner,defaultBranchRef`.
2. List open issues with their number, title, URL, labels, assignees, state, and updated time. Apply any user-supplied label, milestone, number, starting point, or limit.
3. Exclude pull requests, closed issues, issues explicitly marked as blocked, and issues already covered by a merged Pull Request. An issue with an open Pull Request is resumed after inspection; an issue with a closed and unmerged Pull Request stops for intervention rather than receiving a duplicate.
4. Inspect the selected issue with `gh issue view NUMBER --comments` and identify its acceptance criteria, dependencies, and linked work.
5. If appropriate and permitted, assign the issue to the authenticated user before implementation. Assignment is a coordination aid, not proof of completion.
6. Check for an existing branch or Pull Request for the issue before creating anything. Resume a clearly matching open Pull Request instead of opening a duplicate; treat a merged Pull Request as already covered after verifying issue state, and stop for intervention on a closed and unmerged Pull Request. Only when no matching Pull Request exists should the workflow create a new issue-specific branch from the current default branch.

The default branch must be up to date before branching. Use a non-destructive fetch and fast-forward/update operation appropriate to the repository. Do not overwrite local changes or reset the branch.

## Per-Issue Procedure

Repeat these stages for one issue until it is merged or an explicit blocker requires user intervention.

### 1. Prepare an issue branch

- Create or resume a branch named with the issue number and a short sanitized slug, for example `issue/123-add-export-filter`.
- Ensure the branch is based on the current default branch and contains no unrelated changes.
- Do not implement directly on the default branch.
- If an existing branch or Pull Request is found, inspect its commits, changed files, comments, checks, and merge state before deciding whether to continue it.

### 2. Delegate implementation

Start one implementation subagent with a focused prompt that includes:

- The exact issue number, title, URL, body, acceptance criteria, and relevant comments.
- Repository instructions and constraints from `AGENTS.md` and contribution documentation.
- The branch name and the requirement to work only on this issue.
- The expectation to inspect existing source and tests before editing, make the smallest complete change, and add or update tests as appropriate.
- A prohibition on committing, pushing, creating a Pull Request, merging, or changing unrelated files. GitHub lifecycle operations belong to the Pull Request subagent.
- A required report listing changed files, behavior implemented, tests/checks run, and unresolved concerns.

Review the implementation report and workspace diff before continuing. If the implementation is incomplete, send the precise deficiencies to a new implementation subagent rather than proceeding.

### 3. Delegate local verification

After implementation, start a separate verification subagent. It must:

- Review the diff against the exact issue requirements and repository instructions.
- Run focused tests first, then the complete checks required by the repository when practical.
- Inspect for regressions, missing coverage, formatting/type/lint errors, generated artifact requirements, security issues, and unrelated changes.
- Make no code changes, commit, push, or GitHub state changes.
- Return `PASS` only when the implementation is ready for a Pull Request. Return `FAIL` with precise remediation steps otherwise.

If verification fails, use its findings in a new implementation cycle and verify again. Do not create or merge a Pull Request while required local verification is failing.

### 4. Commit, push, and create the Pull Request

When local verification passes, start a Pull Request lifecycle subagent. It must:

- Reinspect the current diff and stage only files belonging to this issue.
- Create a concise, issue-scoped commit. Do not rewrite unrelated history or include unrelated changes.
- Push the issue branch without force-pushing.
- Create exactly one Pull Request targeting the repository default branch. The title should identify the issue, and the body must explain the change, list validation performed, and include a closing reference such as `Fixes #123` when appropriate.
- If a matching open Pull Request already exists, update and use it instead of creating a duplicate. If it is merged, verify the issue state and report it as already covered; if it is closed and unmerged, stop and report the required intervention rather than creating another Pull Request.
- Report the commit SHA, branch, Pull Request number and URL, and the exact pushed revision.

The Pull Request must exist before the issue is considered complete, even if local changes appear correct.

### 5. Validate the Pull Request

The Pull Request lifecycle subagent, or a separate verification subagent if needed, must validate the actual GitHub Pull Request rather than relying only on local results:

- Inspect `gh pr view PR_NUMBER` including the base/head branches, changed files, commits, review status, merge state, and linked issue.
- Inspect the final diff with `gh pr diff PR_NUMBER` and look for scope creep, regressions, missing tests, and accidental secrets or generated files.
- Wait for all required GitHub Actions and repository checks with `gh pr checks PR_NUMBER --watch` when supported. Record the run URLs and final statuses.
- Treat failed, cancelled, skipped-required, pending, unavailable, or unknown required checks as a validation failure. Do not merge based on a local pass when GitHub status is unresolved.
- Confirm the Pull Request is mergeable and has no unresolved merge conflict, required review, or branch-protection blocker.
- Confirm the Pull Request still targets the default branch and is linked to the selected issue.

If validation fails because code or tests need changes, start a new implementation subagent on the existing issue branch, then repeat local verification and Pull Request validation. If it fails because of a transient CI or access problem, wait or report the blocker; never declare success.

### 6. Merge the Pull Request

Only after the Pull Request diff, required checks, reviews, mergeability, and issue linkage have passed:

1. Merge the Pull Request using the repository's permitted non-bypass merge method, normally `gh pr merge PR_NUMBER --squash --delete-branch` when policy allows it.
2. Do not use administrator override, `--admin`, force-push, or a merge-bypass option to overcome protection rules.
3. Confirm the merge completed by rereading the Pull Request state and merge commit with `gh pr view PR_NUMBER`.
4. Confirm the issue was closed by the linked merge. If it remains open, inspect the linkage and repository automation before taking any manual action; do not close an issue merely to make the workflow appear complete.
5. Confirm the merged commit is present on the default branch and that the remote state is current.

If merge fails, leave the Pull Request open, record the exact failure, and resolve it through a new implementation/verification cycle when it is a code, conflict, or check issue. Ask the user for intervention when the blocker is permissions, required human review, or an unavailable external service.

## Loop Termination and Reporting

Continue the per-issue procedure until no eligible issues remain or the user-supplied limit/boundary is reached. At the end, report:

- Each completed issue number and title.
- The issue URL and dedicated Pull Request URL.
- The branch, implementation commit SHA, merge commit SHA when available, and merge method.
- Local checks, GitHub check runs, and their final statuses.
- Any issues skipped, blocked, already in progress, or left open, with the exact reason.
- The next eligible issue when stopping at a user-supplied limit.

Do not report an issue as completed based solely on a created Pull Request, a pushed branch, a passing local test, or an attempted merge.

## Subagent Prompt Templates

### Implementation subagent

> Implement exactly GitHub issue **#[number]: [title]** from [URL]. Read `AGENTS.md`, relevant source, tests, issue comments, and the current diff first. Work only on branch `[branch]`. Make the smallest complete change, add or update tests, and run relevant checks. Do not commit, push, create a Pull Request, merge, or modify unrelated files. Report changed files, behavior implemented, checks run, and blockers.

### Verification subagent

> Verify the implementation for GitHub issue **#[number]: [title]** against its exact acceptance criteria. Do not edit, commit, push, create a Pull Request, or merge. Review the diff, test coverage, repository instructions, and run focused plus required checks. Return `PASS` only if it is ready for a Pull Request; otherwise return `FAIL` with precise remediation steps.

### Pull Request lifecycle subagent

> For GitHub issue **#[number]: [title]**, inspect the verified diff, stage only related files, commit, push branch `[branch]`, and create or resume exactly one Pull Request targeting the default branch. Link it with `Fixes #[number]` when appropriate. Inspect the Pull Request diff and metadata, wait for all required GitHub checks, and confirm mergeability and required reviews. Merge only after every required validation passes, using the permitted non-bypass method. Verify the merged Pull Request and linked issue state. Report commit SHA, Pull Request URL, check URLs/statuses, merge commit, and any blocker. Never force-push or bypass protection rules.

## Safety and Recovery

- Never use destructive Git commands, force-push, administrator merge, or bypass checks without explicit user authorization for that exact action.
- Preserve unrelated working-tree and staging-area changes; keep them out of issue branches and commits.
- Never expose credentials, tokens, or secret values.
- Never open duplicate Pull Requests for the same issue. Search existing branches and Pull Requests first.
- Never merge a Pull Request with failing, pending, skipped-required, or unknown required checks.
- Never close an issue manually solely because a Pull Request was created or because merge automation did not run yet.
- If a subagent reports ambiguity, inspect the repository and issue context, then ask the user only for information that cannot be determined safely.
- If a Pull Request cannot be merged because of access, policy, or required human review, leave it open and report the exact intervention required.
