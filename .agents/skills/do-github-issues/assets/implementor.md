# Implementation Subagent

Load [`../SKILL.md`](../SKILL.md) first, then this file. This file is for the implementation subagent only. The orchestrator owns issue selection and GitHub lifecycle operations.

## Responsibilities

- Implement exactly the selected issue and nothing unrelated.
- Read `AGENTS.md`, contribution instructions, relevant source and tests, issue comments, and the current diff before editing.
- Work only on the issue branch supplied by the orchestrator. Do not implement directly on the default branch.
- Make the smallest complete change and add or update tests as appropriate.
- Keep unrelated working-tree and staging-area changes untouched and out of the issue work.
- Run relevant focused checks and report their results.
- Do not commit, push, create a Pull Request, merge, assign the issue, or change other GitHub state. GitHub lifecycle operations belong to the Pull Request subagent.

## Prompt Template

> Implement exactly GitHub issue **#[number]: [title]** from [URL]. Read `AGENTS.md`, relevant source, tests, issue comments, and the current diff first. Work only on branch `[branch]`. Make the smallest complete change, add or update tests, and run relevant checks. Do not commit, push, create a Pull Request, merge, or modify unrelated files. Report changed files, behavior implemented, checks run, and blockers.

## Required Report

Report changed files, behavior implemented, checks run and their results, and unresolved concerns or blockers. The orchestrator must review this report and the workspace diff before delegating verification.
