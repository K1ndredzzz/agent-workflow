# Dual-Agent Review Convergence Protocol

Applies whenever two agents (e.g., Antigravity and Codex) review, re-review, or iterate on the same codebase—whether manually across interactive sessions or in rework rounds.

## 1. Threat Model & Non-Goals First
Before reviewing or hardening a guardrail, parser, or security-sensitive gate, state the boundary explicitly:
- **In-Scope**: Bugs, crashes, data loss, regressions, false positives that block normal workflows, and **accidental** secret leaks or misuse by humans or coding agents.
- **Out-of-Scope (do not raise as blocking unless the target is explicitly an adversarial sandbox/WAF)**: Deliberate obfuscation by an actor who already holds write/commit access (e.g., crafted multi-commit encoding polyglots, comment/block-scalar spoofing meant to bypass client-side heuristics), speculative style nits, or theoretical races unreachable in normal execution.

## 2. Two-Round Funnel Protocol (Hard Cap)
Bound review scope so each round strictly narrows instead of reopening the whole file:
- **Round 1 — Full Read-Only Audit**: Inspect the full change. Report only **P0/P1** issues backed by both (a) a concrete reproduction and (b) a realistic, non-adversarial trigger scenario. Batch-fix approved items once.
- **Round 2 — Incremental Diff Verification Only**: Inspect **only** `git diff <R1_before>..HEAD`. Verify prior findings are resolved and check only for regressions introduced by those fixes. **Do not reopen untouched pre-existing code or invent new speculative topics.**
- **Round 3+ — Hard Escalation**: If any P0 remains unresolved after Round 2, stop agent-to-agent ping-pong and escalate to the user for a design decision.

## 3. Complexity / Churn Breaker
If the same function or subsystem is patched across **2 consecutive review rounds** with growing heuristic complexity (>30 net lines of edge-case regex/state-machine workarounds), **stop stacking patches** and escalate with three concrete options:
1. **Simplify & fail closed**: Remove the fragile heuristic exemption and require an explicit opt-in marker (e.g., `# secret-scan:allow` or explicit flag).
2. **Delegate to a deterministic tool/parser**: Replace hand-rolled regex parsing with an authoritative parser or dedicated scanner.
3. **Accept & document**: Record the boundary as an explicit Known Limitation under the Threat Model and ship.

## 4. Decouple Audit from Edit (Manual Loops)
In manual dual-agent loops, avoid serial "Agent A edits -> Agent B audits and edits -> Agent A audits B's edits and edits again" amplification:
1. Reviewer produces a **read-only** severity-ranked list (`P0 / P1 / P2`), each item containing reproduction + realistic trigger.
2. User triages which items to fix vs drop as out-of-scope.
3. Implementer applies all approved fixes in one verified pass.

## 5. Approval-Positive Default
When no in-scope P0/P1 defect remains, returning **`APPROVE (no changes needed)`** is the preferred, highest-value outcome. Never manufacture minor hardening edits just to justify a review turn.

## 6. Reusable Convergent Final-Review Prompt Template
Use this template when handing a follow-up diff to another agent (e.g., Codex or Antigravity):

```text
请对 <REPO_PATH> 中 `<BASE_SHA>..HEAD` 的改动做【收敛终审（只读，不直接改代码）】。

硬性边界：
1. 仅审查 `<BASE_SHA>..HEAD` 范围内的改动，禁止挖掘改动范围之外的旧代码问题。
2. 威胁模型：仅防御真实开发与 Agent 自动化中的意外故障、回归、误报和意外泄露；
   明确排除“已有提交权限者刻意构造畸形语法/编码戏法绕过扫描”的对抗场景。
3. 收敛准则：
   - 仅报告同时满足「可复现 + 真实场景可自然触发」的 P0（崩溃/数据丢失/正常用法下门禁失效）或 P1（阻断正常工作的误报/功能退化）。
   - 若修复某问题需要在同一启发式逻辑上继续叠加复杂补丁，不要建议继续打补丁，直接给出“简化/fail-closed 替代方案”。
   - 若不存在满足上述标准的 P0/P1，请直接输出：`APPROVE（无需继续修改）`，不要为产出建议而硬凑问题。
```
