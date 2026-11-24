---
name: dispatch
description: Drive headless coding harnesses (Codex, Antigravity, OpenCode, Pi) via /usr/local/bin/agent-dispatch to implement Linear tickets in isolated git worktrees, run mandatory verification, execute cross-vendor two-axis review, rework up to 2 times, push Draft PRs, and sync Linear to In Review. Use for "/dispatch <ID>", "/dispatch frontier", "/dispatch spec <PARENT_ID>", "/dispatch review", "/dispatch doctor", "/dispatch resume <ID>", "/dispatch status", or "/dispatch cleanup". Not for requirement design (use /grill) or read-only debugging (use /diagnose).
---

# Dispatch (Headless Cross-Harness Orchestrator)

Drive `/usr/local/bin/agent-dispatch` directly from this designer session—no manual window-switching or prompt pasting.

## Routing & Credential Separation
- **Default Worker**: `codex:gpt-6.2-sol@xhigh` (aliases: `codex:sol`, `codex:astra`, `opencode:sol`, `opencode:astra`, `pi:sol`, `pi:astra`, `agy:neon`, `agy:carbon`, `agy:opus`).
- **Default Reviewer (auto cross-vendor)**:
  - GPT-family Worker (`codex` / `opencode` / `pi`) -> **`agy:gemini-4-neon-xhigh`** (two parallel fresh-context axes: Standards + Spec).
  - Antigravity Worker (`agy`) -> **`codex:gpt-6.2-sol@xhigh`**.
- **Credential boundary**: Only `agent-dispatch` itself holds `LINEAR_API_KEY` and invokes `gh` during publish. Headless Worker, Setup, Verify, and Reviewer child processes run with `LINEAR_API_KEY`, `GH_TOKEN`, and `GITHUB_TOKEN` stripped.
- **Orchestrator-owned audit artifacts**: Brief snapshots, baseline standards (`git show <base_sha>:...`), JSON schemas, per-round outputs, and checkpoint state live outside the worktree under `~/.cache/agent-dispatch/runs/<repo_key>/<ID>/<run_id>/`.

## Commands

Run from the target repository root (or pass `--repo /path/to/repo`):

| Intent | Command |
| :--- | :--- |
| Preflight environment & repo checks | `agent-dispatch doctor [--project ...]` |
| Run one Linear ticket (1 initial + up to 2 rework rounds) | `agent-dispatch run <ID> [--worker ...] [--reviewer ...] [--verify-cmd "..."] [--setup-cmd "..."] [--max-rework 2]` |
| Resume / reconcile publish after partial failure (no worker rerun) | `agent-dispatch resume <ID>` |
| Run all ready unblocked `Todo` tickets for this repo | `agent-dispatch frontier [--max-parallel 2] [--worker ...] [--reviewer ...]` |
| Run the current unblocked frontier wave of a Parent Spec | `agent-dispatch spec <PARENT_ID> [--max-parallel 2] [--worker ...] [--reviewer ...]` |
| Standalone heterogeneous Two-Axis Review | `agent-dispatch review [--base origin/main] [--issue <ID>] [--reviewer ...]` |
| Show Linear frontier, blocked tickets, locks, and worktrees | `agent-dispatch status` |
| Prune merged, clean, unlocked worktrees | `agent-dispatch cleanup` |

> **Note on `spec` multi-wave flow**: Each `agent-dispatch spec <PARENT_ID>` invocation runs the **current** unblocked wave and opens Draft PRs (setting Linear to `In Review`). Downstream dependent slices unlock once you merge upstream PRs and mark them `Done` in Linear—then rerun `/dispatch spec <PARENT_ID>` for the next wave.

## Built-In Safety & Quality Gates
1. **Preflight guards**: Verifies repo matches ticket's Linear Project, refuses dirty uncommitted `GLOSSARY.md`/`docs/adr/` in the main repo (so worktrees never miss new rules), acquires per-repo and per-ticket `flock` locks, and refuses parent container or `Done`/`Canceled` tickets.
2. **Dependency bootstrap + base baseline**: Auto-installs missing worktree dependencies (`pnpm`/`npm`/`bun`/`yarn`/`uv`) before starting the worker, and caches a base-commit verify check so pre-existing red builds fail fast as `base_branch_red`.
3. **Mandatory verify + protected paths + deterministic test check**: Missing verify command fails by default (`--allow-no-verify` to opt out). Edits to gate/policy files (`justfile`, `Makefile`, `package.json#scripts`, CI, `CODING_STANDARDS.md`, `docs/adr/*`) or newly added `.skip`/`.only`/`#[ignore]` fail deterministically.
4. **SHA-locked two-axis review**: Orchestrator commits worker changes (scanning staged files against secret patterns), locks `candidate_sha`, runs parallel Standards ([references/review-standards.md](references/review-standards.md)) and Spec ([references/review-spec.md](references/review-spec.md)) reviews against `<base_sha>..<candidate_sha>`, verifies reviewer did not mutate the worktree, and verifies strict JSON schemas.
5. **Rework & failure recovery**: Up to `--max-rework 2` rework rounds (3 total attempts), passing prior review findings forward for convergence and accumulating `discovered_issues` across rounds. Any failure/timeout/block reverts Linear to `Todo` with label `dispatch:failed` and posts redacted diagnostics—never leaving orphaned `In Progress` tickets.
6. **Idempotent Option B delivery**: Fast-forward push of exact `<candidate_sha>` only (never force-pushes over diverged remote commits), creates or updates an **open** Draft PR (`gh pr list --state open`), syncs Linear to `In Review` with mutation verification, and saves stage checkpoints for `agent-dispatch resume <ID>`.

## Designer Handling Rules
- Full runs can take 10–60+ minutes depending on repo size and rework rounds; run via `run_command` with `NotificationTimeoutSeconds: 600` (if still running at 600s, inspect `<run_dir>` logs or `manage_task(status)`—do not poll in a loop).
- Present a crisp summary table on completion: **Ticket | Status | Commit / Draft PR | Worker / Reviewer | Verify | Standards | Spec | Side Issues**.
- **Failure routing**:
  - `worker_blocked` -> clarify the blocker with the user, update the Linear issue, rerun `/dispatch <ID>`.
  - `failed_max_rework` -> inspect `<run_dir>` logs; run **`/diagnose`** if it is a subtle technical bug or **`/slice`** if the ticket was too large.
  - `publish_incomplete` -> fix auth/network and run **`agent-dispatch resume <ID>`** (skips re-implementing).
  - `base_branch_red` -> fix `main` first before dispatching slices.
- If `discovered_issues` is non-empty, list them and ask whether to create follow-up Linear tickets.
- For interactive TDD or manual review inside this session, consult [references/tdd.md](references/tdd.md), [references/review-standards.md](references/review-standards.md), or [references/review-spec.md](references/review-spec.md).
