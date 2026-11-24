---
name: grill
description: Clarify requirements, sharpen domain terms/ADRs, and audit doc conflicts before writing specs or code. Use at the start of any non-trivial feature, refactor, or architecture decision. Not for bug root-cause debugging (use /diagnose) or broad unknown-territory research (use /research).
---

# Grill (Design Alignment + Domain Modeling)

Interview the user in structured rounds until every high-risk branch of the design tree is settled, keeping `GLOSSARY.md` and `docs/adr/` accurate and conflict-free.

## Process

### 1. Ground yourself first
Read `GLOSSARY.md` (or `GLOSSARY-MAP.md`), active files in `docs/adr/`, and relevant source files before asking questions. Use a read-only subagent only when repo exploration is wide enough to crowd your context.

### 2. Audit doc conflicts (Supersede Gate)
Whenever the proposed direction contradicts an existing ADR or glossary term, surface the conflict immediately. Once the user approves the new direction:
- Draft the updated `GLOSSARY.md` and ADR changes (marking superseded ADRs with frontmatter `status: superseded by ADR-NNNN`).
- See [references/domain-docs.md](references/domain-docs.md) for exact formats and the 3-criteria ADR bar.
- **Base-branch handoff rule**: Downstream `/dispatch` worktrees branch from `origin/<default>`. Ensure approved `GLOSSARY.md` / `docs/adr/` updates have landed on the target base branch via your repository's normal authorized commit/PR flow before running `/dispatch`.

### 3. Work the design tree in anti-fatigue rounds
Split open decisions into two buckets:
- **Low-risk / reversible / clear best practice**: decide yourself and present them together as a compact "Assumed defaults" block for 1-word approval.
- **High-risk / hard-to-reverse / genuine trade-off**: ask **at most 2–3 frontier questions per round** whose prerequisites are settled. Number each question and give an opinionated recommended answer worded so "yes" accepts it:

```markdown
**Q1 — <Topic>**: <Concise question + concrete options>
-> Recommended: <Choice and 1-line reason>
```

When designing a high-leverage module interface or testing seam, consult [references/deep-modules.md](references/deep-modules.md).

### 4. Domain discipline
- Challenge fuzzy or overloaded terms against `GLOSSARY.md`; converge on one canonical term.
- Stress-test relationships with concrete edge-case scenarios.
- Cross-reference user claims against actual code; surface contradictions immediately.

### 5. Completion & Next-Step Router
Stop when the high-risk frontier is empty and the user confirms shared understanding. Output a concise **Consensus Summary** (decisions, pending doc updates, open assumptions) and recommend the single best next step:
- Multi-ticket feature -> **`/spec`**
- Small, clear multi-slice change -> **`/slice`**
- Single-slice task -> create one Linear `Todo` issue (`agent-ready`) using [dispatch/references/agent-brief.md](../dispatch/references/agent-brief.md) and run **`/dispatch <ID>`**
- Still too ambiguous to grill -> switch to **`/research`** (Wayfinder mode)
