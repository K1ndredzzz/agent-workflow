---
name: dispatch
description: Drive headless coding harnesses (Codex, Antigravity, OpenCode, Pi) via /usr/local/bin/agent-dispatch to implement Linear tickets in isolated git worktrees, run mandatory verification, execute cross-vendor two-axis review, rework up to 2 times, push Draft PRs, and sync Linear to In Review. Use for "/dispatch <ID>", "/dispatch frontier", "/dispatch spec <PARENT_ID>", "/dispatch review", "/dispatch doctor", "/dispatch resume <ID>", "/dispatch status", or "/dispatch cleanup". Not for requirement design (use /grill) or read-only debugging (use /diagnose).
---

# Dispatch (Headless Cross-Harness Orchestrator)

Drive `/usr/local/bin/agent-dispatch` directly from this designer session—no manual window-switching or prompt pasting.

## Routing & Realistic Boundary Model
- **Default Worker**: `codex:gpt-6.2-sol@xhigh` (aliases: `codex:sol`, `codex:astra`, `opencode:sol`, `opencode:astra`, `pi:sol`, `pi:astra`, `agy:neon`, `agy:carbon`, `agy:argon`, `agy:hydrogen`).
- **Default Reviewer (auto cross-vendor)**:
  - GPT-family Worker (`codex` / `opencode` / `pi`) -> **`agy:gemini-4-neon-xhigh`** (`--mode plan`, two parallel fresh-context axes: Standards + Spec).
  - Antigravity Worker (`agy`) -> **`codex:gpt-6.2-sol@xhigh`** (`--sandbox read-only`).
- **Defense-in-depth child isolation** (under host root / Full Access):
  - Strips `LINEAR_API_KEY`, `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, `GIT_ASKPASS`, `SSH_ASKPASS` (and `CODEX_SUB2API_API_KEY` from non-Codex subprocesses); isolates `GH_CONFIG_DIR` to an empty temp dir, sets sanitized `GIT_CONFIG_GLOBAL` (credential/http-auth/include stripped) + `GIT_TERMINAL_PROMPT=0`, disables repo hooks/fsmonitor/ext-diff on all orchestrator git calls, disables Linear MCP on headless Codex, and sets `AGENT_DISPATCH_CHILD=1` to block nested dispatch.
  - Verifies **SHA-256** integrity of ALL orchestrator-owned artifacts (`brief.md`, `diff-rN.patch`, and every `baseline-standards/*` snapshot) before and after review, checks local `.git/config` + `.gitattributes` + branch/history integrity, verifies worktree `HEAD` + clean status after review, and checks this ticket's remote branch SHA + monotonic `origin/<base>` ancestry + Linear ownership fields (state, assignee, description hash) across worker/verify/review phases.
  - *Boundary transparency*: Because the host runs as `root` with Full Access enabled by user preference (and disk files such as `/etc/environment` remain readable by root), these controls prevent accidental leakage and detect tampering rather than forming an OS-level kernel container boundary. Note also that `codex` (`--sandbox read-only`) and `pi` (`--tools read,grep,find,ls`) enforce tool-level read-only restrictions, whereas `agy` (`--mode plan`) and `opencode` (`--agent plan`) rely on mode discipline (and `agy` shares `~/.gemini/config/mcp_config.json`; pass `--reviewer codex:sol@xhigh` or `--reviewer pi:sol@xhigh` when stricter tool-level read-only isolation is desired).
- **Orchestrator-owned audit artifacts**: Brief snapshots, baseline standards (`git show <base_sha>:...`), `diff-rN.patch`, streaming stdout/stderr logs, `sessions.jsonl`, JSON schemas, and atomic checkpoints live under `~/.cache/agent-dispatch/runs/<repo_key>/<ID>/<run_id>/`. Worktree lock ownership metadata (`owner.json` including `issue_uuid` and `branch`) is persisted inside the repo lock before the worktree becomes visible to `cleanup`/`status`.

## Commands

Run from the target repository root (or pass `--repo /path/to/repo`):

| Intent | Command |
| :--- | :--- |
| Preflight environment, rubrics & repo checks | `agent-dispatch doctor [--project ...]` |
| Run one Linear ticket (1 initial + up to 2 rework rounds) | `agent-dispatch run <ID> [--worker ...] [--reviewer ...] [--verify-cmd "..."] [--setup-cmd "..."] [--max-rework 2] [--worker-timeout 1800] [--review-timeout 900] [--fresh] [--force-rerun] [--skip-baseline] [--allow-branch-only]` |
| Reconcile interrupted Option B publish (stage-aware; read-only verify if completed) | `agent-dispatch resume <ID>` |
| Run all ready unblocked `Todo` + `agent-ready` tickets for this repo | `agent-dispatch frontier [--max-parallel 2] [--limit N] [--worker ...] [--reviewer ...]` |
| Run the current unblocked frontier wave of a Parent Spec | `agent-dispatch spec <PARENT_ID> [--max-parallel 2] [--limit N] [--worker ...] [--reviewer ...]` |
| Standalone heterogeneous review (Standards; add `--issue <ID>` for Spec) | `agent-dispatch review [--base origin/main] [--issue <ID>] [--reviewer ...] [--review-timeout 900]` |
| Show Linear frontier, blocked tickets, locks (`locked`/`unlocked`/`unknown`), and worktrees | `agent-dispatch status` |
| Prune merged, clean, ownership-verified unlocked worktrees (`--all` **force-deletes** unmerged dirty worktrees owned by this repo!) | `agent-dispatch cleanup [--all]` |

> **Note on `spec` / `frontier` multi-wave flow**: Each wave checks both current candidates AND in-flight (`In Progress` / `In Review`) sibling tickets so overlapping parent/child `Touches` paths are serialized across waves. Downstream slices unlock once upstream PRs merge and reach `Done` in Linear—then rerun `/dispatch spec <PARENT_ID>` or `/dispatch frontier`.

## Safety, Quality Gates & Failure Semantics
1. **Preflight guards (before claiming Linear)**: Verifies repo matches ticket's Linear Project, fails closed on unrecognized non-GitHub remotes (unless `--allow-branch-only`), scans ticket title for secrets before invoking any model, refuses unpushed/uncommitted `GLOSSARY.md` (including monorepo `**/GLOSSARY.md`)/`docs/adr/` in main repo, checks remote branch fast-forward compatibility, verifies base-branch baseline (uses a locked unique temporary detached worktree on reused branches if cache misses; fails fast as `base_branch_red`), acquires repo path & canonical ticket `flock` locks, persists `claim_pending`, and re-fetches Linear immediately before claiming `In Progress`.
2. **Mandatory verify + granular protected paths + deterministic test check**: Missing verify command or ambiguous mixed Python pytest/unittest suites (without `pytest` installed) or JS repos with tests but only `lint` fail closed (`--allow-no-verify` to opt out). Unapproved edits to protected gate/policy files (cross-depth `Makefile`/`justfile`/`pyproject.toml`/`pytest.ini`/`ruff.toml`/`Cargo.toml`/`.cargo/*`/`vite.config.*`/`playwright.config.*`/`mypy.ini`/`.coveragerc`/`conftest.py`/`GLOSSARY.md`/`.gitattributes`/`*.mk`/`docs/adr/*`/`.github/workflows/*`/`.github/actions/*`/`package.json` gate sections) or test deletions/skips trigger rework. Ticket-brief exemptions are **fail-closed**, segment-glob scoped (single `*` never crosses `/`), ignore fenced code blocks, and accept ONLY `- gate: <path/glob>` or `- test: <path/glob>` lines under `## Authorized Gate/Test Changes` (or explicit `--allow-gate-edits` / `--allow-test-edits`). All git listings use `-z` (NUL-safe for Chinese/non-ASCII paths).
3. **Full-range secret scan + SHA-locked two-axis review**: Scans ticket/PR title, commit messages, merge commits, and full untruncated `base_sha..candidate_sha` diff before verify/review, locks `candidate_sha`, redacts deleted secret lines in `diff-rN.patch` (blocking added secrets in standalone `review` before any LLM runs), runs parallel Standards ([references/review-standards.md](references/review-standards.md)) and Spec ([references/review-spec.md](references/review-spec.md)) reviews (auto-retrying once on JSON parse / timeout / transient error), and validates strict JSON schemas.
4. **Honest state machine & human-state ownership**:
   - **Pre-publish failures** (`worker_blocked`, `failed_max_rework`, unhandled error) stash any uncommitted dirty worktree edits for clean reruns, preserve human-set states (never overwriting `Done`/`Canceled` or manually moved tickets), and otherwise add `dispatch:failed` **first** before reverting to the pre-claim state + redacted diagnostics with session traces.
   - **Post-push publish interruptions** (`publish_incomplete`): pre-push PR checks validate single-PR + Draft compatibility before `git push`; `push_attempted` is checkpointed before writing remote; `SIGINT`/`SIGTERM`/`SIGHUP` use a lock-free signal flag and preserve the last resumable stage while still writing `dispatch:publish-incomplete` + resume instructions to Linear; `agent-dispatch resume <ID>` performs stage-aware reconciliation (never recreating a human-closed PR, overwriting a human-edited PR body, or reverting human Linear edits when resuming `pr_ready`/`linear_state_synced`, and verifying read-only on `completed`).
   - **`--no-push` local mode** writes to `latest-local.json` (never clobbering `latest.json`) and does not mutate Linear state or comments.

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
