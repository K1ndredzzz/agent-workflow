# Durable Agent Brief Template

Single source of truth for any implementation-ready Linear ticket (used by `/slice`, `/diagnose`, and single-ticket `/grill` outputs).

Write **behavioral contracts**, never fragile line numbers or step-by-step code recipes.

```markdown
## What to Build
End-to-end observable behavior this ticket delivers from the caller/user perspective.

## Key Interfaces & Contracts
- Public types, function signatures, schemas, invariants, and error modes.

## Touches
- Module or shared-resource areas likely modified (e.g., `auth`, `migrations`, `src/routes`, `package.json`). Used to serialize overlapping parallel slices.

## Testing Seam & Acceptance Criteria
- **Seam**: <agreed public boundary>
- **Verify**: <repo check command if non-default, otherwise "repo default">
- [ ] Concrete, independently verifiable criterion 1
- [ ] Concrete, independently verifiable criterion 2

## Authorized Gate/Test Changes
- None (list explicitly only if this ticket is authorized to edit gate/config files or migrate/remove legacy tests)

## Out of Scope
- Explicit non-goals to prevent scope creep
```
