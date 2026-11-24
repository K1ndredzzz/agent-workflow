# TDD Execution Standard (Red -> Green)

Apply whenever changing runtime behavior (skip forcing synthetic tests for pure docs/prose edits).

## Rules of the Loop
1. **Use the brief's seam**: Write tests at the public **seam** specified in the ticket brief. If none is stated, pick the highest existing public interface and note it in your summary.
2. **Red before green**: Write one failing test first, confirm it fails for the expected reason, then write the minimal production change to make it pass.
3. **Vertical micro-cycles**: One test -> one minimal implementation -> repeat.
4. **Regular verification**: Run targeted tests and typecheck during development, and run the full verification command before finishing.

## Good Tests vs Anti-Patterns
- **Good**: Verifies observable behavior through public interfaces; survives internal refactors; expected values come from an independent source of truth (worked example, literal, or spec).
- **Avoid implementation coupling**: Do not mock internal collaborators you control, test private methods, or verify via side-channel DB reads when a public interface exists.
- **Avoid tautological assertions**: Do not recompute expected values using the same formula as the code under test.
- **Mock system boundaries only**: Mock only external APIs, clocks/randomness, or slow I/O boundaries via dependency injection and narrow SDK-style interfaces.
- **Preserve assertion strength**: Never delete, skip, or relax existing tests just to turn a red suite green. (Equivalent test migration is acceptable only when explicitly authorized by the ticket.)
