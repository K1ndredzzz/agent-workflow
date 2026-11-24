---
name: dispatch
description: Drive headless coding harnesses (Codex, Antigravity, OpenCode, Pi) via /usr/local/bin/agent-dispatch to implement Linear tickets in isolated git worktrees, run mandatory verification, execute cross-vendor two-axis review, rework up to 2 times, push Draft PRs, and sync Linear to In Review. Use for "/dispatch <ID>", "/dispatch frontier", "/dispatch spec <PARENT_ID>", "/dispatch review", "/dispatch doctor", "/dispatch resume <ID>", "/dispatch status", or "/dispatch cleanup". Not for requirement design (use /grill) or read-only debugging (use /diagnose).
---

# Dispatch (Headless Cross-Harness Orchestrator)

Drive `/usr/local/bin/agent-dispatch` directly from this designer session—no manual window-switching or prompt pasting.

## Routing & Realistic Boundary Model
- **Default Worker**: `codex:gpt-6.2-sol@xhigh` (aliases: `codex:sol`, `codex:astra`, `opencode:sol`, `opencode:astra`, `pi:sol`, `pi:astra`, `agy:neon`, `agy:carbon`, `agy:opus`).
- **Default Reviewer (auto cross-vendor)**:
  - GPT-family Worker (`codex` / `opencode` / `pi`) -> **`agy:gemini-4-neon-xhigh`** (`--mode plan`, two parallel fresh-context axes: Standards + Spec).
  - Antigravity Worker (`agy`) -> **`codex:gpt-6.2-sol@xhigh`** (`--sandbox read-only`).
- **Defense-in-depth child isolation** (under host root / Full Access):
  - Strips `LINEAR_API_KEY`, `GH_TOKEN`, `GITHUB_TOKEN` (and `CODEX_SUB2API_API_KEY` from non-Codex subprocesses); isolates `GH_CONFIG_DIR` to an empty temp dir, sets sanitized `GIT_CONFIG_GLOBAL` (credential helpers removed) + `GIT_TERMINAL_PROMPT=0`, disables Linear MCP on headless Codex, and sets `AGENT_DISPATCH_CHILD=1` to block nested dispatch.
  - Verifies SHA-256 integrity of orchestrator-owned artifacts (`brief.md`, `baseline-standards/`), verifies worktree `HEAD` + clean status after Review, and checks remote branch / Linear `updatedAt` before & after each harness call.
  - *Note*: Because the host runs as `root` with Full Access enabled by user preference, these controls prevent accidental leakage and detect tampering rather than forming an OS-level kernel container boundary.
- **Orchestrator-owned audit artifacts**: Brief snapshots, baseline standards (`git show <base_sha>:...`), `diff.patch`, streaming stdout/stderr logs, `sessions.jsonl`, JSON schemas, and atomic checkpoints live under `~/.cache/agent-dispatch/runs/<repo_key>/<ID>/<run_id>/`.

## Commands

Run from the target repository root (or pass `--repo /path/to/repo`):

| Intent | Command |
| :--- | :--- |
| Preflight environment & repo checks | `agent-dispatch doctor [--project ...]` |
| Run one Linear ticket (1 initial + up to 2 rework rounds) | `agent-dispatch run <ID> [--worker ...] [--reviewer ...] [--verify-cmd "..."] [--setup-cmd "..."] [--max-rework 2]` |
| Reconcile interrupted Option B publish (no worker rerun) | `agent-dispatch resume <ID>` |
| Run all ready unblocked `Todo` tickets for this repo | `agent-dispatch frontier [--max-parallel 2] [--worker ...] [--reviewer ...]` |
| Run the current unblocked frontier wave of a Parent Spec | `agent-dispatch spec <PARENT_ID> [--max-parallel 2] [--worker ...] [--reviewer ...]` |
| Standalone heterogeneous Two-Axis Review | `agent-dispatch review [--base origin/main] [--issue <ID>] [--reviewer ...]` |
| Show Linear frontier, blocked tickets, locks, and worktrees | `agent-dispatch status` |
| Prune merged, clean, unlocked worktrees | `agent-dispatch cleanup` |

> **Note on `spec` multi-wave flow**: Each `agent-dispatch spec <PARENT_ID>` runs the **current** unblocked wave and opens Draft PRs (setting Linear to `In Review`). Downstream dependent slices unlock once you merge upstream PRs and mark them `Done` in Linear—then rerun `/dispatch spec <PARENT_ID>`.

## Safety, Quality Gates & Failure Semantics
1. **Preflight guards (before claiming Linear)**: Verifies repo matches ticket's Linear Project, refuses unpushed/uncommitted `GLOSSARY.md`/`docs/adr/` in main repo, checks remote branch fast-forward compatibility, checks base-branch verify baseline (fails fast as `base_branch_red` without mutating Linear), and acquires canonical repo & ticket `flock` locks.
2. **Mandatory verify + protected paths + deterministic test check**: Missing verify command fails closed (`--allow-no-verify` to opt out). Unapproved edits to protected gate/policy files (`justfile`, `Makefile`, `package.json#scripts`, `pyproject.toml`, `conftest.py`, CI, `CODING_STANDARDS.md`, `GLOSSARY.md`, `docs/adr/*`, etc.) or newly added test skips/deletions trigger rework (unless explicitly authorized via `## Authorized Gate/Test Changes` in the brief or `--allow-gate-edits` / `--allow-test-edits`).
3. **Full-range secret scan + SHA-locked two-axis review**: Scans the complete `base_sha..candidate_sha` diff (untruncated!) before verify/review, locks `candidate_sha`, runs parallel Standards ([references/review-standards.md](references/review-standards.md)) and Spec ([references/review-spec.md](references/review-spec.md)) reviews with `diff.patch`, and validates strict JSON schemas.
4. **Honest state machine**:
   - **Pre-publish failures** (`worker_blocked`, `failed_max_rework`, unhandled error) revert Linear to `Todo` + label `dispatch:failed` + redacted diagnostics with session traces.
   - **Post-push publish interruptions** (`publish_incomplete`) keep Linear `In Progress` + label `dispatch:publish-incomplete` (never falsely claiming "NOT pushed") so `agent-dispatch resume <ID>` reconciles live remote SHA, open Draft PR (`isDraft`, `base`, `headRefOid`), Linear state, failure-label cleanup, and idempotent summary comment.
   - **`--no-push` local mode** does not mutate Linear state or comments.

## Designer Handling Rules
- Full runs can take 10–60+ minutes; run via `run_command` with `NotificationTimeoutSeconds: 600` (inspect live streaming logs in `<run_dir>/*.log` and `<run_dir>/sessions.jsonl` if still running; do not poll in a loop).
- Present a crisp summary table on completion: **Ticket | Status | Commit / Draft PR | Worker / Reviewer | Verify | Standards | Spec | Side Issues**.
- Always include a **Session Traces** block listing every headless session in `sessions`: render `agy` sessions as clickable `[<role>](conversation://<conversation_id>)` links, show `codex resume <id>` / `opencode -s <id>` / `pi --session <path>` commands, and link `<run_dir>` via `file://`.
- **Failure routing**:
  - `worker_blocked` -> clarify the blocker with the user, update Linear, rerun `/dispatch <ID>`.
  - `failed_max_rework` -> inspect `<run_dir>` logs; run **`/diagnose`** for subtle bugs or **`/slice`** if the ticket was too large.
  - `publish_incomplete` -> resolve network/auth and run **`agent-dispatch resume <ID>`**.
  - `base_branch_red` -> fix the base branch first.
- If `discovered_issues` is non-empty, list them and ask whether to create follow-up Linear tickets.
- For interactive TDD or manual review inside this session, consult [references/tdd.md](references/tdd.md), [references/review-standards.md](references/review-standards.md), or [references/review-spec.md](references/review-spec.md).
