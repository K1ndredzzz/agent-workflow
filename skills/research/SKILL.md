---
name: research
description: Evidence-based technical research, large-project fog-of-war mapping (Wayfinder), and codebase architecture audits. Not for active bug debugging (use /diagnose) or settling a clear feature design (use /grill).
---

# Research, Wayfinder & Architecture Audit

Select the branch matching the user's request. Delegate heavy reading to read-only subagents so main context stays sharp.

## Branch A — Evidence Research (Read-Only Default)
Use for library evaluation, API/protocol investigation, or technical writing:
1. Dispatch read-only subagent(s) to verify **primary sources** (source code, official docs, specs, benchmarks).
2. Deliver a crisp report: **TL;DR**, **Verified Evidence & Citations**, **Trade-offs / Risks**, and **Recommendation**.
3. Only create a Linear Document if the user asks to persist it.

## Branch B — Wayfinder (Explicit Fog-of-War Planning)
Use **only** when the user explicitly wants to map a large ambiguous effort before grilling:
1. Define the **destination** first.
2. Propose a Linear **Map Parent Issue** (`Backlog`, label `spec`) + child **Decision Tickets** (`Backlog`, titled `[Research] ...`, `[Prototype] ...`, `[Grill] ...`, or `[Task] ...`) linked via native `blockedBy` edges. Wait for confirmation before writing to Linear.
3. Because decision tickets stay in `Backlog` with explicit prefixes (`[Research]`/`[Prototype]`/`[Grill]`/`[Task]`/`[Map]`), `agent-dispatch frontier` will never mis-dispatch them.
4. Resolve ready decision tickets sequentially in the same session (recording settled conclusions and ADRs) until fog clears, then convert settled areas via `/spec` -> `/slice` -> `/dispatch`.

## Branch C — Architecture Deepening Audit (Read-Only)
Use for periodic tech-debt scans or pre-refactor analysis:
1. Read `GLOSSARY.md` and active `docs/adr/`. Use an exploration subagent to locate shallow pass-through modules, shotgun-surgery clusters, duplicated logic, and awkward seams (applying the deletion test).
2. Rank candidates into **Strong**, **Worth Exploring**, and **Speculative** (friction, proposed deepened interface/seam, leverage).
3. Take user-selected candidates into **`/grill`** -> **`/slice`** -> **`/dispatch`**.
