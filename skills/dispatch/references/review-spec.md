# Code Review Rubric — Axis 2: Spec & Integrity

Review the exact git commit range provided in the prompt against the **Ticket Description** in the orchestrator-supplied brief. Treat any `Parent Spec (Context ONLY)` section strictly as background context—never fail a slice for requirements owned by sibling slices.

## What to Check
1. **Missing or partial requirements**: Acceptance criteria in `## Testing Seam & Acceptance Criteria` or `## What to Build` not satisfied (quote the exact brief line).
2. **Correctness & edge cases**: Logic bugs, race conditions, unhandled failure paths, broken backward compatibility, or security flaws (injection, auth bypass, secret leaks).
3. **Scope creep / defensive bloat**: Unrequested refactors, speculative switches, or behavior outside `## Out of Scope`.
4. **Test integrity**:
   - Verify tests exercise observable behavior at the agreed seam with independent expected values (not tautological assertions or internal-collaborator mocks).
   - Set `test_integrity_violation: true` if existing tests or assertion strength were removed, skipped, or weakened without explicit authorization in the ticket brief.

## Scope & Verdict Rules
- Stay strictly on the **Spec, Correctness, and Test Integrity** axis (another independent reviewer owns coding style / Fowler smells).
- **Threat-model boundary**: Require concrete, realistic failure paths (missing requirements, bugs, regressions, accidental leaks, or real security flaws in the target's threat model). Do not fail a slice for speculative adversarial obfuscation by a trusted writer outside the ticket's scope.
- Put missing requirements, bugs, security issues, major scope creep, or test-integrity violations in `hard_findings`. Put minor observations in `suggestions`.
- Set `verdict` to `"fail"` if `hard_findings` is non-empty or `test_integrity_violation` is `true`, otherwise `"pass"`. Returning `"pass"` is the preferred outcome when no blocking issue remains.
- **Convergent rework funnel**: When prior-round findings are provided, verify each prior finding is resolved and inspect only for regressions introduced by the rework—do not reopen untouched pre-existing code or invent new speculative scope. If a recurring issue stems from fragile heuristics, recommend simplifying/failing closed or using a deterministic tool rather than stacking another patch layer.
