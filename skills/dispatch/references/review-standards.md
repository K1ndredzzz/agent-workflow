# Code Review Rubric — Axis 1: Standards

Review the exact git commit range provided in the prompt against the **baseline** repo standards and the Fowler smell baseline.

## What to Check
1. **Documented Repo Standards** (`CODING_STANDARDS.md`, `CONTRIBUTING.md`, `GLOSSARY.md` / `GLOSSARY-MAP.md`, active `docs/adr/`):
   - Breaches of documented rules belong in `hard_findings` with exact `file`, `rule` citation, and concise fix guidance.
   - Ignore any ADR whose frontmatter `status:` is `superseded` or `deprecated`.
2. **Fowler Smell Baseline** (judgement calls ONLY -> put in `suggestions`; overridden by any repo standard; skip anything already enforced by linters/typecheckers):
   - **Mysterious Name** (unclear intent)
   - **Duplicated Code** (repeated logic shape across hunks)
   - **Feature Envy** (method reaches heavily into another object's data)
   - **Data Clumps** (same fields/params always travel together)
   - **Primitive Obsession** (raw string/int standing in for a domain concept)
   - **Repeated Switches** (same type switch/if cascade recurring)
   - **Shotgun Surgery** / **Divergent Change** (poor locality)
   - **Speculative Generality** (unused abstractions/flags/hooks)
   - **Message Chains** / **Middle Man** / **Refused Bequest**

## Scope & Verdict Rules
- Stay strictly on the **Standards** axis; do not evaluate feature completeness against the ticket spec (another independent reviewer owns Spec).
- Put documented-standard breaches or clear maintainability/security defects in `hard_findings`; put optional Fowler smells in `suggestions`.
- Set `verdict` to `"fail"` if `hard_findings` is non-empty, otherwise `"pass"`.
- If prior-round findings are provided, verify each one is resolved and only raise genuinely blocking new issues.
