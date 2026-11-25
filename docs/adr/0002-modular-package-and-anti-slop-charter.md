---
status: accepted
---

# ADR-0002: Modular Package Layout, Strict Acyclic Layers, and Anti-Slop Charter

To stop architectural decay in `bin/agent-dispatch` (~6,175 lines) and `tests/test_agent_dispatch.py` (~4,073 lines) while preserving zero third-party runtime dependencies and `/usr/local/bin/agent-dispatch` symlink compatibility, we will decompose the orchestrator into a repository-root Python package (`agent_dispatch/`) organized into six strictly acyclic dependency layers and enforce structural limits via `CODING_STANDARDS.md` and `make verify`.

## Package Layout and Entrypoint

- **Repository-root package (`agent_dispatch/`)**: Lives directly under the repository root so `python3 -B -m unittest discover -s tests` works without `PYTHONPATH` configuration, `pyproject.toml`, or editable installs.
- **Thin executable shim (`bin/agent-dispatch`, ~15 lines)**: Resolves `Path(__file__).resolve().parent.parent`, ensures the repository root is on `sys.path`, and delegates to `agent_dispatch.cli.main()`. This preserves `install.sh` symlinking `/usr/local/bin/agent-dispatch -> <repo>/bin/agent-dispatch` and isolated worktree invocations.
- **Incremental strangler-fig migration, zero permanent god facade**: Modules are extracted bottom-up across small, verifiable slices. Temporary delegation or compatibility re-exports inside `bin/agent-dispatch` are permitted *only* while migration slices are in progress and MUST be removed in the final cutover slice. `agent_dispatch/__init__.py` MUST NOT become a permanent re-export barrel of internal symbols.

## Strict Acyclic Layer Topology (L0–L5)

Imports flow strictly downward. Backward imports, circular imports, and undocumented intra-layer imports are forbidden and will be enforced deterministically by an AST import-graph test in `make verify` once `agent_dispatch/` is created.

| Layer | Modules | Allowed Internal Dependencies | Core Responsibility |
| :--- | :--- | :--- | :--- |
| **L0** | `config` | None (stdlib only) | Configurable roots, rubric preloading, JSON schemas, harness alias table, protected/secret/test pattern registries. |
| **L1** | `secrets` | `config` | Pure text and structured-data redaction, secret filename matching, text secret scanning. Zero subprocess or filesystem I/O. |
| **L2** | `process` | `config`, `secrets` | Lock-free signal and PGID coordination, atomic writes, SHA-256 helpers, `flock` locks, child sandbox env, `git_cmd`, `_BoundedBuffer`, `_RedactedLog`, `run_cmd`, `log`. |
| **L3** | `brief`, `linear`, `git_ops`, `harness` | `config`, `secrets`, `process` (siblings MUST NOT import each other) | `brief`: Agent Brief, Touches, Authorized Gate/Test Changes, ADR status, baseline standards snapshots. `linear`: Linear auth, GraphQL client, project resolution. `git_ops`: repo identity (`canonical_repo_identity`, `repo_cache_key`), `ticket_lock_path`, remote checks, worktree and Worktree Owner (`write_worktree_owner`, `read_worktree_lock_owner`) lifecycle, branch integrity. `harness`: `HarnessSpec`, JSON extraction/schema validation, NDJSON parsers, session traces, Pi session archive, `invoke_harness`. |
| **L4** | `gates` -> `review` -> `publish` | `gates`: L0–L2 + `brief`, `git_ops`. `review`: L0–L2 + `brief`, `harness`, `gates`. `publish`: L0–L2 + `linear`, `git_ops`, `harness`, `review`. | `gates`: verify detection/execution, base baseline cache, protected paths, test integrity diff, git diff/range secret scanning, `orchestrator_commit`. `review`: worker prompt builder, Two-Axis Review runner, review Markdown renderer. `publish`: PR body builder, GitHub PR lookup/feedback, atomic checkpoints, `reconcile_publish`, read-only publish verification. |
| **L5** | `pipeline`, `scheduler`, `commands`, `cli` | `pipeline` (and same-layer stage helpers such as `preflight`/`recovery` if needed): L0–L4. `scheduler`: L0–L3 + `pipeline`. `commands` (and same-layer `maintenance` for `doctor`/`status`/`cleanup` if needed): L0–L4 + `pipeline`, `scheduler`. `cli`: `config`, `secrets`, `process`, `commands`. | Single-ticket pipeline (`execute_ticket` decomposed into focused phase functions), Frontier Wave scheduling and Touches reservation, CLI subcommand handlers, `argparse` entrypoint. |

*Back-edge resolution*: Moving `canonical_repo_identity`, `repo_cache_key`, `ticket_lock_path`, and `read_worktree_lock_owner` into `git_ops`, and keeping `_RedactedLog` alongside `_BoundedBuffer` in `process`, eliminates every cycle present in the legacy monolith.

## Test Decomposition

- Shared test infrastructure (`FakeLinearState`, `start_fake_linear`, `IsolatedDispatchCase`, Git and executable-fixture helpers) moves to `tests/helpers.py`.
- `tests/test_agent_dispatch.py` splits by public behavioral contract into `tests/test_secrets.py`, `tests/test_process_harness.py`, `tests/test_brief_linear.py`, `tests/test_gates.py`, `tests/test_review_publish.py`, `tests/test_scheduler_pipeline.py`, `tests/test_e2e_cli.py`, `tests/test_install.py`, and `tests/test_live_linear.py`, plus `tests/test_architecture.py` for the AST dependency DAG gate.
- Tests import target modules directly by their public seams rather than loading `bin/agent-dispatch` via `SourceFileLoader`.

## Anti-Slop Charter and Transitional Exemption

- Detailed enforceable rules live in `CODING_STANDARDS.md` (new/refactored modules `<= 600` lines, functions `<= 100` lines, deep modules over shallow pass-through wrappers, strict DAG, public-seam tests).
- **Transitional exemption**: Legacy `bin/agent-dispatch` and `tests/test_agent_dispatch.py` are exempt from file-length and function-length caps *only* while incremental extraction slices remain in flight. New feature logic must not be added to their unsplit monolith sections, and the exemption terminates when the final cutover slice lands.

## Considered Options

- **Repo-root `agent_dispatch/` + thin `bin/agent-dispatch` (chosen)**: Zero packaging boilerplate, immediate compatibility with `unittest discover` and `/usr/local/bin/agent-dispatch` symlinks, clean module boundaries.
- **`src/agent_dispatch/` layout (rejected)**: Standard for wheel distribution, but adds `PYTHONPATH` plumbing across `bin/agent-dispatch`, `Makefile`, and tests with no benefit for a symlink-installed zero-dependency CLI.
- **Permanent `agent_dispatch/__init__.py` re-exporting all 63 legacy symbols (rejected)**: Minimizes test edits in the short term, but recreates a god namespace that undermines strict layering and encourages coupling to internals.

## Consequences

- Each refactoring slice touches one cohesive layer and its corresponding contract tests, keeping `main` continuously deployable via Draft PRs and `make verify`.
- `Makefile` `verify` will evolve during the extraction slices to compile all Python modules under `agent_dispatch/`, `bin/`, and `tests/` and execute the AST DAG check automatically.
