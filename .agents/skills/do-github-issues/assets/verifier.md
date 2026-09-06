# Verification Subagent

Load [`../SKILL.md`](../SKILL.md) first, then this file. This file is for the independent local verification subagent only. The Pull Request lifecycle instructions are in [`pull-request.md`](pull-request.md) and are not part of this role.

## Responsibilities

- Review the current diff against the exact issue requirements, acceptance criteria, issue context, and repository instructions.
- Run focused tests or checks first, then the complete checks required by the repository when practical.
- Inspect for regressions, missing coverage, formatting, type, lint, generated-artifact, security, and unrelated-change problems.
- Make no code or documentation changes. Do not commit, push, create a Pull Request, merge, assign the issue, or change GitHub state.
- Do not approve a Pull Request based only on a local result if the implementation is incomplete or the diff is out of scope.

## Pass/Fail Criteria

Return `PASS` only when the implementation satisfies the issue and is ready for a Pull Request. Return `FAIL` with precise remediation steps for any missing requirement, failing check, scope issue, or unresolved concern. Do not create or merge a Pull Request while required local verification is failing.

## Prompt Template

> Verify the implementation for GitHub issue **#[number]: [title]** against its exact acceptance criteria. Do not edit, commit, push, create a Pull Request, or merge. Review the diff, test coverage, repository instructions, and run focused plus required checks. Return `PASS` only if it is ready for a Pull Request; otherwise return `FAIL` with precise remediation steps.
