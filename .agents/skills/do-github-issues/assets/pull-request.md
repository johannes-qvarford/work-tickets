# Pull Request Lifecycle

Load [`../SKILL.md`](../SKILL.md) first, then this file. This file is for the Pull Request lifecycle subagent only. It owns issue-scoped staging, commit, push, Pull Request creation or resumption, GitHub validation, merge, and post-merge confirmation. It must not bypass repository protections.

## Commit, Push, and Create

Start only after the separate verifier returns `PASS`. Reinspect the current diff and stage only files belonging to this issue. Create a concise issue-scoped commit, push without force-pushing, and create exactly one Pull Request targeting the default branch. The title must identify the issue; the body must explain the change, list local validation, and include a closing reference such as `Fixes #123` when appropriate.

If a matching open Pull Request already exists, use it rather than creating a duplicate. If it is merged, verify the issue state and report it as already covered. If it is closed and unmerged, stop and report the exact Pull Request and required intervention.

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

Replace the `git add` paths, Pull Request title, and the two Pull Request body template lines with the actual issue details before running the commands. If the branch already has a matching open Pull Request, do not run `gh pr create`; use its number instead. A merged or closed/unmerged matching Pull Request follows the lifecycle rules above and never gets a duplicate.

Report the commit SHA, branch, Pull Request number and URL, and exact pushed revision.

## Pull Request Validation

For Pull Request number `PR`, inspect metadata and the exact GitHub diff before waiting for checks:

```sh
: "${REPO:?Set REPO before inspecting a Pull Request}"
PR=123
: "${PR:?Set PR before inspecting a Pull Request}"
gh pr view "$PR" --repo "$REPO" \
  --json number,title,url,state,isDraft,baseRefName,headRefName,mergeStateStatus,reviewDecision,statusCheckRollup,commits,files,closingIssuesReferences
gh pr diff "$PR" --repo "$REPO"
gh pr checks "$PR" --repo "$REPO" --watch
```

Re-run `gh pr view` after checks finish to confirm the base and head, merge state, reviews, checks, changed files, commits, and issue linkage still match the selected work. Inspect the diff for scope creep, regressions, missing tests, accidental secrets, and generated files. Record check run URLs and final statuses.

Treat a failed, cancelled, skipped-required, pending, unavailable, or unknown required check as a failure. Confirm that the Pull Request is mergeable, has no unresolved conflict or branch-protection blocker, targets the default branch, and is linked to the selected issue. If validation fails because code or tests need changes, return the precise findings for a new implementation cycle on the existing branch. If it fails because of a transient CI or access problem, wait or report the blocker; never declare success.

## Merge and Post-Merge Confirmation

Only after the Pull Request diff, required checks, reviews, mergeability, and issue linkage have passed, use the repository's permitted non-bypass merge method:

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

Do not use `--admin`, force-push, or a merge-bypass option. Confirm the merge completed by rereading the Pull Request state and merge commit, confirm the issue was closed by the linked merge, and confirm the merged commit is present on the default branch and the remote state is current. If the issue remains open, inspect linkage and repository automation; do not close it manually merely to make the workflow appear complete. If merge or confirmation fails, leave the Pull Request open and report the exact command failure.

## Prompt Template

> For GitHub issue **#[number]: [title]**, inspect the verified diff, stage only related files, commit, push branch `[branch]`, and create or resume exactly one Pull Request targeting the default branch. Link it with `Fixes #[number]` when appropriate. Inspect the Pull Request diff and metadata, wait for all required GitHub checks, and confirm mergeability and required reviews. Merge only after every required validation passes, using the permitted non-bypass method. Verify the merged Pull Request and linked issue state. Report commit SHA, Pull Request URL, check URLs/statuses, merge commit, and any blocker. Never force-push or bypass protection rules.
