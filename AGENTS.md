# Repository Engineering Rules (`agent-workflow`)

Extends the global Antigravity/Codex engineering rules with repository-specific governance for self-hosted development of `agent-workflow`.

## Authoritative Docs (Read Before Editing)
- [CODING_STANDARDS.md](CODING_STANDARDS.md) — Hard rules (`CS-1`–`CS-7`) enforced by the headless Standards Reviewer.
- [GLOSSARY.md](GLOSSARY.md) — Canonical domain terms and forbidden aliases.
- [docs/adr/0001-threat-model-and-non-goals.md](docs/adr/0001-threat-model-and-non-goals.md) — Threat model, Non-Goals, and secret-scan convergence policy.
- [docs/adr/0002-modular-package-and-anti-slop-charter.md](docs/adr/0002-modular-package-and-anti-slop-charter.md) — `agent_dispatch/` L0–L5 DAG architecture, test split, and transitional exemptions.

## Non-Negotiable Constraints
- **Zero third-party dependencies (`CS-1`)**: Python 3 stdlib + documented host CLIs (`git`, `gh`, harnesses) only.
- **Strict L0–L5 acyclic layers (`CS-2`)**: Follow ADR-0002's allowed-dependency table; no backward/circular imports and no permanent god re-export barrel.
- **Deep modules (`CS-3`)**: New/refactored modules under `agent_dispatch/` must be `<= 600` lines and functions `<= 100` lines (unsplit legacy `bin/agent-dispatch` and `tests/test_agent_dispatch.py` carry a transitional exemption until migration completes).
- **Threat model & complexity breaker (`CS-4`)**: Defend against accidental bugs, false positives, and accidental leaks; do not add adversarial anti-obfuscation parsers or stack >30 net lines of heuristic edge-case patches across consecutive rounds.
- **Verification & `main` hygiene**: Run `make verify` before finishing. Keep `/root/.agent/workflow` on Last Known Good `main`; land changes via feature branches and Draft PRs, then upgrade locally with `git pull --ff-only && ./install.sh`.
