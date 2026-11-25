# agent-workflow

A local multi-agent engineering workflow and headless ticket orchestrator that coordinates design alignment, specification slicing, isolated implementation, deterministic verification, independent review, and draft pull request delivery.

## Language

**Agent Brief**:
The self-contained implementation contract on an actionable ticket, defining scope, acceptance criteria, declared Touches, and any explicit gate or test change authorizations.
_Avoid_: Prompt Dump, Raw Ticket Text, Spec Copy

**Authorized Gate/Test Changes**:
Explicit, narrowly scoped permissions declared in an Agent Brief (or operator flags) that allow a ticket to edit otherwise protected configuration or test expectations.
_Avoid_: Gate Bypass, Blanket Exemption, Test Override

**Baseline Standards**:
The immutable snapshot of repository standards, glossary terms, and active architectural decisions captured from the verified base revision before a worker runs, used as the sole standards reference during review.
_Avoid_: Live Repo Docs, Working-Tree Rules, Prompt Guidelines

**Candidate SHA**:
The exact reviewed commit revision produced on a ticket's branch that has passed verification, secret scanning, and integrity checks and is locked as the single revision eligible for review and publication.
_Avoid_: Working HEAD, Draft Commit, Unverified Tip

**Claim**:
The verified transition of a ticket into active execution ownership, recorded only after preflight checks succeed and live ticket state confirms no human or competing process has intervened.
_Avoid_: Ticket Lock, Soft Reservation, Optimistic Start

**Draft PR Delivery (Option B)**:
The orchestrator's remote publication model: pushing the verified branch, opening or updating a Draft Pull Request, advancing the ticket to review state, and posting a deduplicated delivery summary while leaving final merge authority to a human.
_Avoid_: Auto-Merge, Direct Push to Main, Publish-Only Mode

**Frontier Wave**:
A single conflict-free batch of unblocked, ready tickets whose declared Touches do not overlap with each other or with currently active work, selected for safe concurrent execution.
_Avoid_: Full Backlog Run, Serial Queue, Parallel Batch

**Harness**:
An external coding-agent CLI backend (such as Codex, Antigravity, OpenCode, or Pi) driven headlessly by the orchestrator in either implementation or read-only review roles.
_Avoid_: Agent Runtime, CLI Wrapper, Model Backend

**Protected Paths**:
Repository governance, build, verification, and CI policy locations that an implementation worker may not modify unless granted Authorized Gate/Test Changes.
_Avoid_: Immutable Files, Config Blacklist, Restricted Files

**Rework Round**:
A bounded follow-up implementation pass triggered when verification, deterministic gates, or Two-Axis Review reports blocking defects, culminating in a fresh Candidate SHA and re-verification.
_Avoid_: Retry Attempt, Blind Rerun, Fix Loop

**Run Directory**:
The orchestrator-owned audit location for a single execution attempt, holding immutable snapshots, redacted logs, session traces, review diffs, and recovery checkpoints.
_Avoid_: Scratch Folder, Temp Logs, Worktree Cache

**Touches**:
The repository paths or directory scopes declared on a ticket as its expected edit surface, used to prevent overlapping concurrent tickets from colliding within a Frontier Wave.
_Avoid_: Modified Files, Ownership Globs, Conflict List

**Two-Axis Review**:
Independent, cross-vendor read-only evaluation of a locked Candidate SHA along two separate dimensions—repository standards compliance and ticket specification fulfillment—each returning a structured pass/fail verdict.
_Avoid_: Single-Pass Audit, Self-Review, Style Check

**Worktree Owner**:
The durable ownership record binding an isolated managed worktree to a specific repository, ticket identity, and branch so status, recovery, and cleanup never touch unowned or competing worktrees.
_Avoid_: Branch Lock, Active Runner, Stale Marker
