# Repository Engineering Rules (`agent-workflow`)

Extends the global Antigravity/Codex engineering rules with repository-specific governance for self-hosted development of `agent-workflow`.

## Authoritative Docs (Read Before Editing)
- [CODING_STANDARDS.md](CODING_STANDARDS.md) — Hard rules (`CS-1`–`CS-7`) enforced by the headless Standards Reviewer.
- [GLOSSARY.md](GLOSSARY.md) — Canonical domain terms and forbidden aliases.
- [docs/adr/0001-threat-model-and-non-goals.md](docs/adr/0001-threat-model-and-non-goals.md) — Threat model, Non-Goals, and secret-scan convergence policy.
- [docs/adr/0002-modular-package-and-anti-slop-charter.md](docs/adr/0002-modular-package-and-anti-slop-charter.md) — `agent_dispatch/` L0–L5 DAG architecture, test split, and transitional exemptions.

## Non-Negotiable Constraints
- **Zero third-party dependencies (`CS-1`)**: Python 3 stdlib + documented host CLIs (`git`, `gh`, harnesses) only.
- **Strict L0–L5 acyclic layers (`CS-2`)**: Follow ADR-0002's allowed-dependency table; keep `agent_dispatch/__init__.py` docstring-only and `bin/agent-dispatch` a thin launcher (`<= 40` lines).
- **Deep modules (`CS-3`)**: Every module under `agent_dispatch/` must be `<= 600` lines and every function `<= 100` lines (enforced by `tests/test_architecture.py`).
- **Threat model & complexity breaker (`CS-4`)**: Defend against accidental bugs, false positives, and accidental leaks; do not add adversarial anti-obfuscation parsers or stack >30 net lines of heuristic edge-case patches across consecutive rounds.
- **Test fixtures & imports (`CS-7`)**: Import shared fixtures as `from tests.helpers import ...` (never bare `helpers`), and construct synthetic high-confidence secret or skip-marker test strings dynamically at runtime so test files do not trip staged secret or test-integrity gates.
- **Verification & `main` hygiene**: Run `make verify` before finishing. Keep `/root/.agent/workflow` on Last Known Good `main`; land changes via feature branches and Draft PRs, then upgrade locally with `git pull --ff-only && ./install.sh`.
