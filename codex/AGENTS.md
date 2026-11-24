# Global Engineering Rules (Codex)

Applies unless a repository-local `AGENTS.md` overrides a specific rule.

- **Minimal complete change**: Solve only the active task's acceptance criteria. Leave unrelated files, formatting, and adjacent modules untouched.
- **No scope drift**: Record independent bugs or cleanup opportunities in your final report instead of fixing them inline.
- **No defensive bloat**: Avoid speculative abstractions, unrequested config flags, or broad `try/catch` blocks that swallow errors.
- **Behavior-first testing**: When changing runtime behavior, add or update tests at the public seam (Red -> Green) and preserve existing assertion strength.
- **Read context safely**: Read `GLOSSARY.md` (or `GLOSSARY-MAP.md`), active `docs/adr/` (ignore `superseded`/`deprecated` status), and only the specific brief named in the current prompt. Never print or commit secrets.
- **Git boundary**: Never run `git push`, modify `main`/`master`, or deploy unless explicitly requested. Under `agent-dispatch`, leave committing to the orchestrator.
