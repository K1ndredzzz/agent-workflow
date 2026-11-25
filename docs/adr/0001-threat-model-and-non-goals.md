---
status: accepted
---

# ADR-0001: Orchestrator Threat Model, Non-Goals, and Secret-Scan Convergence

`agent-dispatch` is a local developer orchestrator running on a `root` host with Full Access to prevent accidental engineering failures and secret leaks—not an OS-level adversarial sandbox. Consequently, guardrails and secret scanners are scoped to realistic non-adversarial risks, and the ~200-line hand-rolled YAML anchor/alias secret-scanning state machine will be replaced with a simpler fail-closed rule plus a strictly bounded inline opt-in marker.

## Threat Model Boundary

### In-Scope
- Accidental bugs, crashes, data loss, and functional regressions in orchestration, verification, review, and delivery.
- Workflow-blocking false positives in deterministic gates (protected paths, test integrity, secret scanning).
- Accidental secret leaks or unsafe Git/Linear mutations caused by humans or headless coding harnesses during normal workflows.
- Gate bypasses reachable through normal usage (for example, UTF-16 config files, binary diffs, or stale checkpoints).
- Recovery consistency across signals (`SIGINT`/`SIGTERM`/`SIGHUP`), network outages, and concurrent runs.

### Out-of-Scope (Non-Goals)
- **OS or kernel sandboxing**: The host runs as `root` with Full Access by user preference; child environment stripping and artifact SHA-256 verification provide defense-in-depth and tamper detection, not kernel containment.
- **Adversarial obfuscation by actors with write/commit access**: Deliberately crafted comment/block-scalar spoofs, multi-commit encoding polyglots, or syntax tricks designed to evade client-side regex scanners are explicitly out of scope.
- **Speculative hardening**: Theoretical races or synthetic edge cases without a realistic, non-adversarial reproduction trigger.

## Secret-Scanning Convergence Strategy

In a dedicated follow-up implementation slice, `agent-dispatch` will **simplify and fail closed** on YAML alias credential references instead of maintaining a hand-rolled YAML lexer:

1. **Remove hand-rolled YAML anchor tracking**: Delete the YAML prefix/anchor/alias parser (`_yaml_prefix_state`, `_yaml_alias_binding`, `_has_yaml_alias_or_anchor`, `_scan_contextual_additions`) and the second-pass `-U2147483647` full-context diff rescan.
2. **Retain high-signal accidental-leak coverage**: Keep high-confidence token patterns (`SECRET_VALUE_PATTERNS`), PEM private-key detection, live process credential environment checks, UTF-8/UTF-16 decoding, and standard placeholder exemptions (`<PLACEHOLDER>`, `${ENV_VAR}`, `{{VAR}}`).
3. **Fail closed on YAML alias credentials with a tiered `# secret-scan:allow` opt-in**:
   - Generic credential headers whose values are YAML aliases fail closed by default.
   - A line-level `# secret-scan:allow` comment may opt out **only** generic heuristic credential rules (such as generic `X-API-Key`, `Authorization`, `Bearer`, `Basic`, or YAML alias references) during source/diff secret scanning.
   - `# secret-scan:allow` MUST **never** exempt high-confidence secret token formats (`SECRET_VALUE_PATTERNS`), PEM private keys, or live credential values present in process environment variables.
   - `# secret-scan:allow` MUST **never** disable `redact_secrets()` when writing logs, session traces, snapshots, PR descriptions, or Linear comments.

## Complexity and Churn Breaker

Per `skills/dispatch/references/review-convergence.md`, if any heuristic guardrail or parser accumulates more than 30 net lines of edge-case workarounds across two consecutive review rounds, stop stacking patches and choose one of: (a) simplify and fail closed, (b) delegate to a deterministic parser if compatible with repo dependencies, or (c) document the boundary as a known limitation.

## Considered Options

- **Simplify and fail closed with tiered `# secret-scan:allow` (chosen)**: Unloads ~200 lines of brittle adversarial-parser state machine before modularization, preserves zero third-party dependencies, and guarantees real tokens cannot leak even on allow-tagged template lines.
- **Freeze existing ~200-line YAML anchor logic as-is (rejected)**: Avoids touching existing tests immediately, but carries an already-churned adversarial regex state machine and full-context diff rescan permanently into the refactored package.
- **Use a deterministic YAML parser or external secret scanner (rejected)**: Python's standard library has no YAML parser; adding PyYAML or requiring an external scanner CLI breaks the zero third-party runtime dependency contract.

## Consequences

- Security and gate modules remain compact, predictable, and reviewable under the 2-round convergence funnel.
- Rare legitimate YAML alias header examples or synthetic test fixtures can use standard placeholders or `# secret-scan:allow` without reopening YAML syntax parsing.
- Follow-up implementation slices will remove the legacy YAML anchor/alias tests that only verified adversarial comment/block-scalar spoofing and replace them with focused fail-closed and tiered `# secret-scan:allow` contract tests.
