# Domain Docs: GLOSSARY.md & ADRs

Create files lazily—only when the first domain term or ADR is needed. Commit and push accepted doc changes before running `/dispatch` so isolated worktrees see the current rules.

## GLOSSARY.md
Location: repo root `GLOSSARY.md` (or `GLOSSARY-MAP.md` pointing to per-context `GLOSSARY.md` in multi-context monorepos).

```markdown
# {Context Name}

{1–2 sentence description of this context.}

## Language

**CanonicalTerm**:
{1–2 sentence definition of what it IS, with zero implementation details.}
_Avoid_: Alias1, Alias2
```

Rules:
- Pick one canonical term per concept and list synonyms under `_Avoid_`.
- Include only project-specific domain terms, not generic programming terms.

## ADRs (`docs/adr/NNNN-slug.md`)
Write an ADR **only** when all three hold:
1. **Hard to reverse** (meaningful migration cost)
2. **Surprising without context** (future reader would ask "why?")
3. **Genuine trade-off** (real alternatives were rejected for specific reasons)

Template (keep the `status:` line clean—no trailing comments listing other status values):
```markdown
---
status: accepted
---

# {Short decision title}

{1–3 sentences: context, decision, and why.}
```
Valid `status` values: `accepted`, `deprecated`, `superseded by ADR-NNNN`. Include `Considered Options` or `Consequences` only when non-obvious.

## Supersede Gate
When a new architectural decision invalidates an older ADR:
1. Change the old ADR's frontmatter `status:` to `superseded by ADR-NNNN` and add a 1-line pointer at the top.
2. Write the new ADR referencing the old one.
3. Update `GLOSSARY.md` if domain boundaries changed.
4. Commit and merge/push these doc changes before starting downstream implementation slices (reverting a cancelled architecture is then a clean `git revert`).
