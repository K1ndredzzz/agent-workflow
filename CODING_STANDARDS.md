# Coding Standards (`agent-workflow`)

Enforceable repository standards for human contributors, coding workers, and the headless Standards Reviewer (`skills/dispatch/references/review-standards.md`). Breaches of **Hard Rules** (`CS-1`–`CS-7`) belong in `hard_findings`.

## Hard Rules (Blocking)

### CS-1: Zero Third-Party Runtime Dependencies
- Production code under `bin/` and `agent_dispatch/` MUST import only Python 3 standard library modules and invoke only documented host CLIs (`git`, `gh`, and configured harness binaries).
- Do NOT introduce `pip` packages, `requirements.txt`, or third-party runtime imports.

### CS-2: Strict Acyclic Layer Architecture (ADR-0002)
- Every module under `agent_dispatch/` MUST obey the L0–L5 allowed-dependency table in `docs/adr/0002-modular-package-and-anti-slop-charter.md`.
- Backward imports, circular imports, and sibling cross-imports inside L3 (`brief`, `linear`, `git_ops`, `harness`) are forbidden.
- Do NOT create a permanent god re-export barrel in `agent_dispatch/__init__.py` or `bin/agent-dispatch` (temporary migration shims inside `bin/agent-dispatch` are allowed only until the final cutover slice removes them).

### CS-3: Deep Module Size and Cohesion Limits
- Every newly created or refactored Python module under `agent_dispatch/` MUST be **<= 600 lines**.
- Every newly created or refactored function or method under `agent_dispatch/` MUST be **<= 100 lines**.
- Prefer deep modules (meaningful behavior behind a small, stable interface) over shallow pass-through wrappers that merely forward calls.
- *Transitional exemption*: Unsplit legacy files `bin/agent-dispatch` and `tests/test_agent_dispatch.py` are exempt from file and function length caps while incremental extraction slices are in flight, provided no new feature logic is added to their monolith sections.

### CS-4: Threat Model and Complexity Breaker (ADR-0001)
- Security gates and secret scanners MUST target realistic non-adversarial threats defined in `docs/adr/0001-threat-model-and-non-goals.md` (bugs, crashes, regressions, false positives, and accidental leaks).
- Do NOT add hand-rolled parsers or edge-case heuristics aimed at defeating deliberate obfuscation by actors who already hold write/commit access.
- If a heuristic rule or scanner accumulates more than 30 net lines of edge-case patches across two consecutive review rounds, replace it with a fail-closed rule or documented limitation instead of stacking further patches.

### CS-5: Secret Hygiene and Fail-Closed Security Invariants
- All persisted or externally emitted diagnostics—streaming logs, session traces, baseline snapshots, PR bodies, and Linear comments—MUST pass through secret redaction before writing or sending.
- Inline `# secret-scan:allow` exemptions (once implemented per ADR-0001) MUST apply only to generic heuristic header/alias checks during source/diff scanning; they MUST NEVER exempt high-confidence token patterns (`SECRET_VALUE_PATTERNS`), PEM private keys, live process credential environment values, or output redaction.
- Every orchestrator-issued Git subprocess MUST be constructed via `git_cmd()` (disabling repository hooks, fsmonitor, pager, and path quoting).
- Child harness subprocess environments MUST be constructed via `build_child_env()` so host credentials remain stripped.

### CS-6: Atomic Persistence and Lock-Free Signal Safety
- Checkpoints, Worktree Owner metadata, baseline cache entries, and immutable review artifacts MUST be written atomically (`atomic_write_text()`).
- Signal handlers (`SIGINT`, `SIGTERM`, `SIGHUP`) MUST stay strictly lock-free: never acquire `threading.Lock`/`RLock`, call `Event.set()`, or perform blocking I/O inside a signal handler.

### CS-7: Public-Seam Testing and Domain Terminology
- New and migrated tests MUST import modules under `agent_dispatch.*` directly and assert behavior through public interfaces.
- New tests MUST NOT `mock.patch` private (`_`-prefixed) internal helpers unless isolating an OS or external process boundary with no public seam; never weaken or delete existing assertions without explicit authorization.
- Code symbols, CLI diagnostics, and docs MUST use canonical domain terms from `GLOSSARY.md` and avoid listed `_Avoid_` aliases.
- Respect all active ADRs under `docs/adr/`; ignore any ADR whose frontmatter `status:` is `superseded` or `deprecated`.

## Deterministic Verification Gate

- Every change MUST pass `make verify` cleanly with zero warnings or leftover bytecode artifacts (`PYTHONDONTWRITEBYTECODE=1`).
- Once `agent_dispatch/` is introduced, `make verify` MUST compile all Python files under `agent_dispatch/`, `bin/`, and `tests/`, and run the AST import-DAG architecture test enforcing `CS-2`.

## Review Conventions (Suggestions, Non-Blocking Unless Violating CS-1–CS-7)

- Keep helper abstractions local until a second real caller exists (no speculative flags, unused hooks, or dummy adapters).
- Prefer returning structured results and explicit error states over mutating hidden module globals.
- Keep `install.sh` idempotent and non-destructive; preserve `/usr/local/bin/agent-dispatch` symlink compatibility.
