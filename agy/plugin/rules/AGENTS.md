# Global Engineering & Review Convergence Rules (Antigravity)

Applies globally unless a repository-local `GEMINI.md` / `AGENTS.md` overrides a specific rule.

- **Convergent dual-agent reviews**: Follow `/root/.agent/skills/dispatch/references/review-convergence.md` when auditing, re-auditing, or drafting cross-agent review prompts (e.g., for Codex). State the realistic threat model (accidental bugs, regressions, realistic misuse, and accidental leaks—excluding deliberate writer obfuscation unless the target is explicitly a security sandbox/WAF). Enforce the 2-round funnel (Round 1 full read-only audit -> Round 2 incremental diff verification only, no reopening untouched code). Prefer returning `APPROVE (no changes needed)` when no in-scope P0/P1 remains.
- **Complexity / churn breaker**: If the same function or subsystem churns across 2 consecutive review rounds with growing heuristic workarounds (>30 net lines), stop stacking patches and escalate to the user with simplification options (fail-closed + explicit allowlist, deterministic parser/tool, or documented limitation).
- **Decouple audit from edit**: In manual dual-agent audit loops, default to read-only findings backed by reproduction + realistic non-adversarial trigger (or bounded diff-only verification on follow-up rounds) unless the user explicitly asks to audit and fix in one pass.
- **Threat model before implementation**: When designing guardrails, parsers, or security-sensitive gates (e.g., in `/grill` or `/spec`), settle explicit In-Scope threats and Out-of-Scope non-goals before coding.
