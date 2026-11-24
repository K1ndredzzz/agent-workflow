# Durable Agent Brief Template

Single source of truth for any implementation-ready Linear ticket (used by `/slice`, `/diagnose`, and single-ticket `/grill` outputs).

Write **behavioral contracts**, never fragile line numbers or step-by-step code recipes.

```markdown
## What to Build
End-to-end observable behavior this ticket delivers from the caller/user perspective.

## Key Interfaces & Contracts
- Public types, function signatures, schemas, invariants, and error modes.

## Touches
- Module or shared-resource areas likely modified (e.g., `auth`, `migrations`, `src/routes`, `package.json`). List literal resource names or repository-relative paths. A parent path conflicts with all its descendants across batches.

## Testing Seam & Acceptance Criteria
- **Seam**: <agreed public boundary>
- **Verify**: repo default (if a custom check command is needed, pass `--verify-cmd "..."` explicitly to `/dispatch`; ticket prose is never executed as shell)
- [ ] Concrete, independently verifiable criterion 1
- [ ] Concrete, independently verifiable criterion 2

## Authorized Gate/Test Changes
- None (fail-closed: to authorize specific protected files or test migrations, use explicit lines ONLY: `- gate: <path/glob>` or `- test: <path/glob>`)

## Out of Scope
- Explicit non-goals to prevent scope creep
```
