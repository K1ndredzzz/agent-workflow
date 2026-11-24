---
name: retro
description: Conduct a retrospective on recent sessions, agent-dispatch runs, or PRs to upgrade deterministic guardrails, CODING_STANDARDS.md, and lean agent docs. Not for debugging a single failure (use /diagnose).
---

# Retro (Environment & Guardrail Improvement)

Inspect recent session transcripts, `~/.cache/agent-dispatch/runs/` logs (`sessions.jsonl`, `*.log`, `state.json`), git history, or PR review findings and upgrade the engineering environment based on failure frequency, miss cost, and false-positive risk.

## Hierarchy of Fixes
1. **Deterministic Guardrails FIRST (for recurring mechanical errors)**: Turn repeated mechanical failures (banned API, broken wiring, missing format/lint/typecheck/test command) into linter rules, pre-commit hooks, or repo verify scripts—weighing maintenance and false-positive cost rather than automating one-off flukes.
2. **`CODING_STANDARDS.md` SECOND (judgement calls)**: Document conventions linters cannot check (architectural boundaries, naming craft, domain consistency). Keep high-priority repo rules discoverable via `AGENTS.md` so implementation workers do not get blindsided during review.
3. **Lean `AGENTS.md` & Skills THIRD**: Keep `AGENTS.md` minimal—navigation pointers and non-obvious gotchas only.

## Applying Approved Governance Changes
- Because verify scripts, linters, `CODING_STANDARDS.md`, and `docs/adr/*` are **protected paths** under `/dispatch`, apply approved repo-governance edits directly in the Designer session (or on a dedicated ticket with explicit `## Authorized Gate/Test Changes` / `--allow-gate-edits`).
- Global skill and `agent-dispatch` updates live in `/root/.agent/workflow` (git-tracked with unit & E2E tests): run `python3 -m unittest discover -s /root/.agent/workflow/tests -v` before committing.

## Agent-Doc Writing Rules
- **Single source of truth**: keep each rule in one file and link via sharp pointers (`what it is` + `when to open it`).
- **Progressive disclosure**: keep top-level steps compact; push branch-specific reference into `references/`.
- **State positive targets**: describe the desired behavior directly; pair any unavoidable hard guardrail with the positive action.
- **Prune sediment**: delete lines duplicating config files (`package.json`, `Makefile`) or instructions the model already follows by default.

## Output
Present concrete improvement candidates ordered by severity and ROI, then apply the approved ones cleanly.
