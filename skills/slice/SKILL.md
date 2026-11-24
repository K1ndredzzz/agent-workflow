---
name: slice
description: Break a Linear Spec or settled design into small, end-to-end verifiable vertical slices with durable Agent Briefs and native Linear blockedBy edges. Use after /spec or directly after /grill for small clear features. Not for executing tickets (use /dispatch).
---

# Slice (Vertical Tickets -> Linear)

Decompose a Spec or settled design into **tracer-bullet vertical slices** and publish them as linked Linear child issues via the global `linear` MCP.

## Global Linear Convention
Resolve Team (from `list_teams`) and Project (from `.linear-project` or git repo name match in `list_projects`) automatically.

## Process

### 1. Gather context
Use current conversation context, or if given a Linear issue ID (e.g., `K1N-10`), fetch it via `get_issue` + `list_comments`. Respect `GLOSSARY.md` and active `docs/adr/`. Identify prefactoring slices first ("make the change easy, then make the easy change").

### 2. Draft vertical slices
- Each slice cuts a narrow, **end-to-end verifiable** path across all required layers (schema -> logic -> API/UI -> seam test), targeting roughly **1 public seam, 1–3 modules, and <= ~400 lines of diff** so a fresh headless worker finishes reliably.
- **Wide refactor exception**: sequence mechanical repo-wide refactors as **expand -> migrate batches -> contract** so checks stay green at every slice.
- Declare **blocking edges** (`Blocked by`) only for genuine technical gates.
- Declare **`Touches`** (module/resource-level, e.g., `migrations`, `routes`, `package.json`, `auth`) so `/dispatch` knows which parallel slices share hot files and should run serially.

### 3. Confirm breakdown (1 round)
Present a compact table: **# | Title | Blocked by | Touches | Deliverable**. Wait for user confirmation before publishing.

### 4. Publish to Linear (atomically ready)
1. Create tickets in topological order (blockers first) via `save_issue` with `parentId` (when slicing a Spec), `project`, native **`blockedBy`** relations, and initial state **`Backlog`** while building the graph.
2. Each ticket description **must** follow [dispatch/references/agent-brief.md](../dispatch/references/agent-brief.md).
3. Verify the created parent links and `blockedBy` edges by reading back the issues, then transition all ready slices to state **`Todo`** and apply label **`agent-ready`**. Leave the Parent Spec issue in `Backlog`.

### 5. Completion & Next-Step Router
Print the Linear IDs, dependency graph, and immediately runnable **Frontier**. Recommend **`/dispatch frontier`** or **`/dispatch <ID>`**.
