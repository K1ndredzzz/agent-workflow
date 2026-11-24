---
name: diagnose
description: Read-only root-cause investigation for bugs, test failures, crashes, or regressions. Produces a verified Root Cause Report and proposes creating a Linear Bug ticket without editing production source code. Not for implementing fixes (use /dispatch) or broad library research (use /research).
---

# Diagnose (Root Cause Investigation)

Investigate failures rigorously and stop at a verified root-cause report. **Do not edit production source code**—actual TDD fixes belong in `/dispatch`.

## Allowed Side Effects
- Running read-only inspection commands, existing tests, and isolated repro scripts written under `/tmp/repro-<slug>/`.
- Avoid mutating production databases, shared state, or tracked repo files.

## Process

1. **Reproduce tightly**: Run the failing test or build a minimal repro under `/tmp/repro-<slug>/`. Delegate massive log mining to a read-only subagent to keep context clean.
   - **If non-reproducible locally** (e.g., production-only environment or missing telemetry): stop cleanly and report **Insufficient Evidence** with the exact logs, metrics, or instrumentation needed next.
2. **Isolate root cause**: Trace the real code path and recent `git log` (for regressions). Prove the mechanism with concrete evidence—never guess.
3. **Redact secrets**: Strip tokens, API keys, passwords, and private headers before printing or writing to Linear.
4. **Deliver a Root Cause Report formatted as an Agent Brief** (compatible with [dispatch/references/agent-brief.md](../dispatch/references/agent-brief.md)):
   - Include: `Symptom`, `Reproduction`, `Root Cause (with evidence)`, `Blast Radius`, followed by `## What to Build`, `## Key Interfaces & Contracts`, `## Touches`, `## Testing Seam & Acceptance Criteria`, and `## Out of Scope`.
5. **Next-Step Router**:
   - Ask whether to publish this as a Linear Bug Issue (via global `linear` MCP, matched to this repo's Linear Project, initial state **`Backlog`** so `frontier` does not auto-run it before you review).
   - Once approved and moved to `Todo` (or explicitly named), recommend **`/dispatch <ID>`** (or **`/slice <ID>`** for multi-module fixes).
