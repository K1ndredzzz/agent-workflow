# Deep Modules & Interface Design

Shared vocabulary for designing interfaces and testing seams:

- **Module**: any unit with an interface and implementation (function, class, package, slice).
- **Interface**: everything a caller must know—types, invariants, ordering constraints, error modes, configuration, performance.
- **Depth**: leverage at the interface—large behaviour behind a small surface area. Prefer **deep** modules over thin pass-through **shallow** modules.
- **Seam**: the public boundary where callers and tests observe behaviour and where external dependencies can be swapped without editing internals.
- **Adapter**: a concrete implementation satisfying an interface at a seam.

## Core Principles
1. **Deletion test**: if deleting the module makes complexity vanish, it is a pass-through; if complexity reappears across callers, it earns its keep.
2. **Interface = test surface**: callers and tests cross the same seam.
3. **Avoid speculative seams**: introduce an interface seam only when external I/O isolation or actual implementation variance justifies it—never add dummy adapters for hypothetical future needs.
4. **Testable shape**: accept external dependencies via injection, return results instead of mutating hidden state, keep surface area small.

## Design It Twice (only for high-risk, high-leverage interfaces)
1. Summarize constraints and dependencies for the user.
2. Spawn 2–3 parallel read-only subagents with contrasting goals (minimal surface, common-caller ergonomics, ports & adapters).
3. Compare on **depth**, **locality**, and **seam placement**, then give one opinionated recommendation.
