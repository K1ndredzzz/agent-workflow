---
name: spec
description: Synthesize settled design consensus into a durable Parent Spec published to Linear via the global Linear MCP. Use right after /grill for multi-ticket features. Not for requirement discovery (use /grill) or ticket breakdown (use /slice).
---

# Spec (Global Linear Specification)

Synthesize already-settled design consensus into a durable Parent Spec on **Linear** via the global `linear` MCP.

## Global Linear Convention (Zero Per-Repo Config)
- Use global `linear` MCP tools (`list_teams`, `list_projects`, `save_issue`).
- Resolve the target Project automatically: check repo root `.linear-project`; otherwise match git repo name against `list_projects` (case-insensitive). If no project exists yet, ask once to create it under the workspace's default team from `list_teams`.

## Process

1. **Ground in repo docs**: Respect `GLOSSARY.md` (or `GLOSSARY-MAP.md`) and active `docs/adr/`.
2. **Verify testing seams**: Reuse seams already agreed during `/grill`. Only pause for a single confirmation if seams were not yet settled or if independent persistence/compatibility contracts need an additional seam.
3. **Draft the Spec** using the template below. Keep it behavioral and durable (no fragile line numbers; include a prototype type/schema snippet only when it expresses an invariant more precisely than prose).
4. **Publish to Linear as a Parent Container**:
   - Create (or update if re-running for the same feature) the Parent Issue in the matched Project with state **`Backlog`** and label **`spec`** (so `agent-dispatch frontier` never mis-dispatches an unsliced Spec as a coding ticket).
5. **Next-Step Router**: Print the Linear Spec ID + URL and recommend: **`/slice <ID>`**.

## Spec Template

```markdown
## Problem Statement
{User-facing problem being solved.}

## Solution
{User-facing solution.}

## User Stories
1. As a <actor>, I want <capability>, so that <outcome>.

## Implementation Decisions
- Modules to build/modify, public interfaces, invariants, and error modes
- Schema changes, API contracts, and ADR references

## Testing Decisions
- Agreed public seams and what each seam verifies
- Prior art for tests in this repo

## Out of Scope
- Explicit non-goals
```
