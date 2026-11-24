---
name: retro
description: Conduct a retrospective on recent sessions, agent-dispatch runs, or PRs to upgrade deterministic guardrails, CODING_STANDARDS.md, and lean agent docs. Not for debugging a single failure (use /diagnose).
---

# Retro (Environment & Guardrail Improvement)

Inspect recent session transcripts, `~/.cache/agent-dispatch/runs/` logs, git history, or PR review findings and upgrade the engineering environment based on failure frequency, miss cost, and false-positive risk.

## Hierarchy of Fixes
1. **Deterministic Guardrails FIRST (for recurring mechanical errors)**: Turn repeated mechanical failures (banned API, broken wiring, missing format/lint/typecheck/test command) into linter rules, pre-commit hooks, or repo verify scripts—weighing maintenance and false-positive cost rather than automating one-off flukes.
2. **`CODING_STANDARDS.md` SECOND (judgement calls)**: Document conventions linters cannot check (architectural boundaries, naming craft, domain consistency). Keep high-priority repo rules discoverable via a short pointer in `AGENTS.md` so implementation workers do not get blindsided during review.
3. **Lean `AGENTS.md` & Skills THIRD**: Keep `AGENTS.md` minimal—navigation pointers and non-obvious gotchas only. Since custom skills and `agent-dispatch` live under version control (`/root/.agent/workflow`), review diffs cleanly and commit changes after verification.

## Audit Checklist
- **Automated checks**: missing/unwired verify scripts, flaky tests, slow bootstrap.
- **Review misses & false positives**: patterns where reviewers missed real bugs or rejected valid changes.
- **Navigation & context waste**: places agents burned turns hunting for files, commands, or logs.
- **Doc hygiene**: stale ADRs missing `superseded` status, duplicated rules across global/repo docs, no-op instructions.

## Agent-Doc Writing Rules
- **Single source of truth**: keep each rule in one file and link via sharp pointers (`what it is` + `when to open it`).
- **Progressive disclosure**: keep top-level steps compact; push branch-specific reference into `references/`.
- **State positive targets**: describe the desired behavior directly; pair any unavoidable hard guardrail with the positive action.
- **Prune sediment**: delete lines duplicating config files (`package.json`, `Makefile`) or instructions the model already follows by default.

## Output
Present concrete improvement candidates ordered by severity and ROI, then apply the approved ones cleanly.
