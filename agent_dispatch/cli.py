"""Argument parsing and entrypoint for agent-dispatch."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Sequence

from . import commands
from .process import check_shutdown, install_signal_handlers
from .secrets import redact_secrets, safe_json


def _positive_int(raw: str) -> int:
    val = int(raw)
    if val <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {val})")
    return val


def _rework_range_int(raw: str) -> int:
    val = int(raw)
    if val < 0 or val > 5:
        raise argparse.ArgumentTypeError(f"--max-rework must be between 0 and 5 (got {val})")
    return val


def add_exec_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", help="Target git repo path (default: current directory)")
    p.add_argument("--project", default=None, help="Explicit Linear project name or ID")
    p.add_argument("--worker", default="codex:sol@xhigh", help="Worker harness spec (default: codex:sol@xhigh)")
    p.add_argument("--reviewer", default=None, help="Reviewer harness spec (default: auto cross-vendor)")
    p.add_argument("--verify-cmd", default=None, help="Override deterministic verification command")
    p.add_argument("--setup-cmd", default=None, help="Override dependency bootstrap command for fresh worktree")
    p.add_argument("--max-rework", type=_rework_range_int, default=2, help="Max rework rounds after initial attempt (0..5, default: 2)")
    p.add_argument("--verify-timeout", type=_positive_int, default=900, help="Verify timeout seconds (default: 900)")
    p.add_argument("--setup-timeout", type=_positive_int, default=600, help="Bootstrap timeout seconds (default: 600)")
    p.add_argument("--worker-timeout", type=_positive_int, default=1800, help="Worker harness timeout seconds (default: 1800)")
    p.add_argument("--review-timeout", type=_positive_int, default=900, help="Review harness timeout seconds (default: 900)")
    p.add_argument("--no-push", action="store_true", help="Local dry run: skip Linear mutations, push, and PR")
    p.add_argument("--allow-no-verify", action="store_true", help="Allow passing when no verify command is detected")
    p.add_argument("--allow-gate-edits", action="store_true", help="Allow worker edits to protected gate/policy files")
    p.add_argument("--allow-test-edits", action="store_true", help="Allow authorized test deletions/migrations")
    p.add_argument("--allow-dirty-docs", action="store_true", help="Skip preflight check for uncommitted/unpushed domain docs")
    p.add_argument("--allow-branch-only", action="store_true", help="Allow branch-only delivery on non-GitHub remotes without Draft PR")
    p.add_argument("--skip-baseline", action="store_true", help="Skip base-commit verify baseline check")
    p.add_argument("--skip-project-check", action="store_true", help="Skip cross-repo Linear project match guard")
    p.add_argument("--force-rerun", action="store_true", help="Allow rerunning an issue already In Progress or In Review")
    p.add_argument("--fresh", action="store_true", help="Recreate worktree and branch cleanly from base (recovers dirty trees)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-dispatch",
        description="Production-grade cross-harness Linear -> Worktree -> Verify -> Heterogeneous Review -> Draft PR orchestrator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run full closed-loop pipeline for one Linear ticket")
    p_run.add_argument("issue", help="Linear issue identifier (e.g., K1N-12)")
    add_exec_flags(p_run)
    p_run.set_defaults(func=commands.cmd_run)

    p_resume = sub.add_parser("resume", help="Reconcile interrupted Option B publish from checkpoint")
    p_resume.add_argument("issue", help="Linear issue identifier (e.g., K1N-12)")
    p_resume.add_argument("--repo", default=None, help="Target git repo path")
    p_resume.add_argument("--project", default=None, help="Explicit Linear project name or ID")
    p_resume.add_argument("--skip-project-check", action="store_true", help="Skip cross-repo project check")
    p_resume.set_defaults(func=commands.cmd_resume)

    p_front = sub.add_parser("frontier", help="Dispatch non-overlapping wave of ready Todo tickets")
    p_front.add_argument("--max-parallel", type=_positive_int, default=2, help="Max parallel worktrees (default: 2)")
    p_front.add_argument("--limit", type=_positive_int, default=None, help="Max tickets to dispatch in this wave")
    add_exec_flags(p_front)
    p_front.set_defaults(func=commands.cmd_frontier)

    p_spec = sub.add_parser("spec", help="Dispatch current ready unblocked wave of a parent Spec issue")
    p_spec.add_argument("parent_issue", help="Parent Linear Spec identifier (e.g., K1N-10)")
    p_spec.add_argument("--max-parallel", type=_positive_int, default=2, help="Max parallel worktrees (default: 2)")
    p_spec.add_argument("--limit", type=_positive_int, default=None, help="Max tickets to dispatch in this wave")
    add_exec_flags(p_spec)
    p_spec.set_defaults(func=commands.cmd_spec)

    p_rev = sub.add_parser("review", help="Run standalone SHA-locked heterogeneous review (Standards + optional Spec)")
    p_rev.add_argument("--repo", default=None, help="Target git repo path")
    p_rev.add_argument("--base", default=None, help="Base ref to diff against (default: origin/<default>)")
    p_rev.add_argument("--issue", default=None, help="Optional Linear issue ID for Spec axis")
    p_rev.add_argument("--reviewer", default="agy:neon", help="Reviewer harness spec (default: agy:neon)")
    p_rev.add_argument("--review-timeout", type=_positive_int, default=900, help="Review harness timeout seconds (default: 900)")
    p_rev.set_defaults(func=commands.cmd_review)

    p_stat = sub.add_parser("status", help="Inspect Linear frontier, zombie runs, locks, and worktrees")
    p_stat.add_argument("--repo", default=None, help="Target git repo path")
    p_stat.add_argument("--project", default=None, help="Explicit Linear project name or ID")
    p_stat.set_defaults(func=commands.cmd_status)

    p_doc = sub.add_parser("doctor", help="Preflight environment, Linear, GitHub, and repo checks")
    p_doc.add_argument("--repo", default=None, help="Target git repo path")
    p_doc.add_argument("--project", default=None, help="Explicit Linear project name or ID")
    p_doc.set_defaults(func=commands.cmd_doctor)

    p_clean = sub.add_parser("cleanup", help="Remove merged, clean, unlocked worktrees for this repo")
    p_clean.add_argument("--repo", default=None, help="Target git repo path")
    p_clean.add_argument("--all", action="store_true", help="Force remove all unlocked worktrees even if unmerged")
    p_clean.set_defaults(func=commands.cmd_cleanup)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if os.environ.get("AGENT_DISPATCH_CHILD") == "1":
        print(
            safe_json({"status": "error", "error": "Nested agent-dispatch invocation blocked (AGENT_DISPATCH_CHILD=1)."}),
            file=sys.stderr,
        )
        return 2

    install_signal_handlers()
    args = _build_parser().parse_args(argv)
    try:
        status = args.func(args)
        check_shutdown()
        return status
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    except Exception as exc:
        print(safe_json({"status": "error", "error": redact_secrets(str(exc))}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2
