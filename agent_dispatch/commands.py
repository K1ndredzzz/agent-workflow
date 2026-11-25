"""Execution, recovery, and standalone review subcommands."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .brief import render_issue_snapshot, snapshot_baseline_standards
from .config import _cache_root, _worktree_cache_path, assert_required_rubrics_loaded
from .git_ops import (
    canonical_repo_identity,
    detect_default_branch,
    fetch_base_ref,
    get_git_common_dir,
    get_repo_root,
    repo_cache_key,
    ticket_lock_path,
)
from .harness import default_reviewer_for, parse_harness_spec
from .linear import LinearClient, resolve_linear_auth, resolve_project
from .maintenance import cmd_cleanup, cmd_doctor, cmd_status
from .pipeline import execute_ticket
from .process import atomic_write_text, file_lock, git_cmd, log, run_cmd, sha256_file
from .publish import (
    COMPLETED_STAGES,
    PUBLISH_STAGES,
    reconcile_publish,
    require_publish_ownership,
    save_checkpoint,
    verify_completed_publish_readonly,
)
from .review import run_two_axis_review
from .scheduler import collect_runnable_tickets, extract_active_touches, run_batch_safely
from .secrets import redact_secrets, safe_json


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = get_repo_root(args.repo)
    client = LinearClient(resolve_linear_auth())
    repo_project = None if args.skip_project_check else resolve_project(client, repo_root, args.project)
    worker = parse_harness_spec(args.worker)
    reviewer = parse_harness_spec(args.reviewer) if args.reviewer else default_reviewer_for(worker)
    if worker.vendor == reviewer.vendor:
        log(f"Warning: worker ({worker.label}) and reviewer ({reviewer.label}) share vendor '{worker.vendor}'.")
    res = execute_ticket(
        client,
        repo_root,
        repo_project,
        args.issue,
        worker,
        reviewer,
        verify_cmd_override=args.verify_cmd,
        setup_cmd_override=args.setup_cmd,
        max_rework=args.max_rework,
        verify_timeout=args.verify_timeout,
        setup_timeout=args.setup_timeout,
        worker_timeout=args.worker_timeout,
        review_timeout=args.review_timeout,
        no_push=args.no_push,
        allow_no_verify=args.allow_no_verify,
        allow_gate_edits=args.allow_gate_edits,
        allow_test_edits=args.allow_test_edits,
        allow_dirty_docs=args.allow_dirty_docs,
        allow_branch_only=args.allow_branch_only,
        skip_baseline=args.skip_baseline,
        skip_project_check=args.skip_project_check,
        force_rerun=args.force_rerun,
        fresh=args.fresh,
    )
    print(safe_json(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") in ("passed", "passed_local", "passed_branch_only") else 1


def cmd_resume(args: argparse.Namespace) -> int:
    """Verify the checkpoint and resume under the canonical ticket lock."""
    repo_root = get_repo_root(args.repo)
    rkey = repo_cache_key(repo_root)
    cur_repo_identity = canonical_repo_identity(repo_root)
    repo_common = get_git_common_dir(repo_root)
    client = LinearClient(resolve_linear_auth())
    repo_project = None if args.skip_project_check else resolve_project(client, repo_root, args.project)

    issue = client.get_issue(args.issue)
    if not args.skip_project_check and repo_project:
        if (issue.get("project") or {}).get("id") != repo_project["id"]:
            raise RuntimeError(f"Cross-repo guard: {issue['identifier']} does not belong to project '{repo_project['name']}'.")

    t_lock = ticket_lock_path(repo_root, issue["id"])
    with file_lock(t_lock, blocking=False):
        latest_file = _cache_root() / "runs" / rkey / issue["identifier"] / "latest.json"
        run_dir, state = _load_resume_checkpoint(latest_file, issue, cur_repo_identity)
        if state.get("stage") in ("claim_pending", "claim_released"):
            return _release_interrupted_claim(client, issue, run_dir, latest_file, state)
        if state.get("stage") in ("rollback_pending", "rollback_completed"):
            return _resume_rollback(client, issue, run_dir, latest_file, state)

        _verify_resume_publish_evidence(repo_root, repo_common, rkey, issue, state)
        live_issue = client.get_issue(issue["id"])
        if (live_issue.get("state") or {}).get("type") in ("completed", "canceled"):
            raise RuntimeError(f"{live_issue['identifier']} is already {live_issue['state']['name']}; nothing to resume.")

        if state.get("stage") in COMPLETED_STAGES:
            result = verify_completed_publish_readonly(client, live_issue, repo_root, state)
            print(safe_json(result, indent=2, ensure_ascii=False))
            return 0

        reconcile_publish(
            client,
            live_issue,
            repo_root,
            run_dir,
            latest_file,
            state,
            allow_branch_only=bool(state.get("allow_branch_only")),
        )

    print(
        safe_json(
            {
                "issue": issue["identifier"],
                "status": "passed" if state.get("stage") == "completed" else "passed_branch_only",
                "reconciled": True,
                "commit": state["candidate_sha"],
                "pr_url": state.get("pr_url", ""),
                "linear_state": (live_issue.get("state") or {}).get("name"),
                "run_dir": str(run_dir),
                "sessions": state.get("sessions", []),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _load_resume_checkpoint(
    latest_file: Path, issue: dict[str, Any], cur_repo_identity: str,
) -> tuple[Path, dict[str, Any]]:
    if not latest_file.is_file():
        raise RuntimeError(f"No checkpoint found at {latest_file}")
    try:
        latest_data = json.loads(latest_file.read_text(encoding="utf-8"))
        if not isinstance(latest_data, dict) or not latest_data.get("run_dir"):
            raise ValueError("missing run_dir")
        run_dir = Path(latest_data["run_dir"])
        state_file = run_dir / "state.json"
        # Prefer authoritative run_dir/state.json over latest.json index to avoid split-brain on mid-write crash!
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else latest_data
        if not isinstance(state, dict):
            raise ValueError("state is not a JSON object")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Corrupt checkpoint at {latest_file}: {exc}") from exc
    if state.get("issue_uuid") and state["issue_uuid"] != issue["id"]:
        raise RuntimeError(f"Checkpoint issue UUID mismatch ({state['issue_uuid']} != {issue['id']}).")
    if state.get("repo_identity") and state["repo_identity"] != cur_repo_identity:
        raise RuntimeError(
            f"Checkpoint repo_identity mismatch ({state['repo_identity']} != {cur_repo_identity}); refusing cross-repo resume."
        )
    if state.get("no_push"):
        raise RuntimeError(f"Latest run for {issue['identifier']} was `--no-push` local mode; rerun without `--no-push` to publish.")
    return run_dir, state


def _release_interrupted_claim(
    client: LinearClient, issue: dict[str, Any], run_dir: Path, latest_file: Path, state: dict[str, Any],
) -> int:
    live = client.get_issue(issue["id"])
    if state["stage"] == "claim_pending":
        pre_state_id = state.get("pre_claim_state_id")
        if not pre_state_id:
            raise RuntimeError("Interrupted claim checkpoint missing pre_claim_state_id; human state preserved.")
        if live["state"]["id"] != pre_state_id:
            # Older claim_pending records lack an assignee/description snapshot.
            if not state.get("claimed_ownership") or live["state"]["id"] != state.get("claimed_state_id"):
                raise RuntimeError("Cannot verify ownership of interrupted claim; human state preserved.")
            require_publish_ownership(live, state)
            client.ensure_label_and_add(live, "dispatch:failed")
            live = client.get_issue(issue["id"])
            require_publish_ownership(live, state)
            client.update_issue_state_by_id(live, pre_state_id)
        state["stage"] = "claim_released"
        save_checkpoint(run_dir, latest_file, state)
    print(safe_json({"issue": issue["identifier"], "status": "claim_released", "rerun": f"agent-dispatch run {issue['identifier']}"}))
    return 0


def _resume_rollback(
    client: LinearClient, issue: dict[str, Any], run_dir: Path, latest_file: Path, state: dict[str, Any],
) -> int:
    pre_state_id = state.get("pre_claim_state_id")
    pre_state_name = state.get("pre_claim_state_name") or "Todo"
    if state["stage"] == "rollback_pending":
        live = client.get_issue(issue["id"])
        if not state.get("rollback_state_reverted"):
            if not state.get("claimed_ownership") or not state.get("claimed_state_id"):
                raise RuntimeError("Cannot verify ownership of pending rollback; human state preserved.")
            require_publish_ownership(live, state)
            # CRITICAL ORDERING: add `dispatch:failed` label FIRST, THEN revert state!
            client.ensure_label_and_add(live, "dispatch:failed")
            live = client.get_issue(issue["id"])
            require_publish_ownership(live, state)
            if pre_state_id:
                client.update_issue_state_by_id(live, pre_state_id, label_hint=pre_state_name)
            else:
                client.update_issue_state(live, "Todo")
            state["rollback_state_reverted"] = True
            save_checkpoint(run_dir, latest_file, state)
        rollback_marker = f"<!-- agent-dispatch-rollback:{state.get('run_id', '')} -->"
        existing_comments = (live.get("comments") or {}).get("nodes", [])
        if not any(rollback_marker in (c.get("body") or "") for c in existing_comments):
            sess_lines = [
                f"- `{s.get('role')}` ({s.get('harness')}): `{s.get('resume_cmd') or s.get('conversation_link') or s.get('stdout_log')}`"
                for s in (state.get("sessions") or [])
            ]
            reason_text = state.get("failure_reason") or state.get("error") or "Pre-publish failure (rollback recovered via resume)."
            comment_parts = [
                rollback_marker,
                f"### [agent-dispatch] Halted -> Reverted to `{pre_state_name}` (`dispatch:failed`, recovered via resume)",
                f"- **Worktree (local, NOT pushed by this run)**: `{state.get('worktree', '')}` (`{state.get('branch', '')}`)",
                f"- **Run Logs**: `{run_dir}`",
                f"- **Reason**:\n{redact_secrets(str(reason_text))[:2500]}",
            ]
            if sess_lines:
                comment_parts += ["", "**Sessions:**", *sess_lines]
            client.add_comment(live["id"], "\n".join(comment_parts))
        state["stage"] = "rollback_completed"
        state.pop("rollback_error", None)
        save_checkpoint(run_dir, latest_file, state)
    print(
        safe_json(
            {
                "issue": issue["identifier"],
                "status": "rollback_completed",
                "linear_state": pre_state_name,
                "rerun": f"agent-dispatch run {issue['identifier']}",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _verify_resume_publish_evidence(
    repo_root: Path, repo_common: Path, rkey: str, issue: dict[str, Any], state: dict[str, Any],
) -> None:
    if (state.get("stage") not in (*PUBLISH_STAGES, *COMPLETED_STAGES)
            or (state.get("review") or {}).get("passed") is not True
            or (state.get("verify") or {}).get("passed") is not True
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", state.get("candidate_sha") or "")
            or state.get("branch") != f"feat/{issue['identifier'].lower()}"):
        raise RuntimeError(f"Checkpoint at {state.get('stage')!r} lacks verified publish evidence.")
    worktree = _worktree_cache_path(rkey, issue["identifier"].upper())
    if Path(state["worktree"]) != worktree:
        raise RuntimeError("Checkpoint worktree differs from the managed ticket path.")
    if not (worktree / ".git").exists():
        raise RuntimeError(f"Worktree missing at {worktree}; cannot reconcile.")
    if get_git_common_dir(worktree) != repo_common:
        raise RuntimeError(f"Worktree {worktree} does not belong to repository {repo_root}.")


def cmd_frontier(args: argparse.Namespace) -> int:
    repo_root = get_repo_root(args.repo)
    client = LinearClient(resolve_linear_auth())
    proj = resolve_project(client, repo_root, args.project)
    issues = client.list_project_issues(proj["id"], state_type="unstarted")
    started = client.list_project_issues(proj["id"], state_type="started")
    active_touches, active_unscoped = extract_active_touches(started)
    runnable, blocked, skipped = collect_runnable_tickets(client, issues)
    if not runnable:
        print(safe_json({"project": proj["name"], "status": "empty_frontier", "blocked": blocked, "skipped": skipped}, indent=2, ensure_ascii=False))
        return 0
    results, deferred = run_batch_safely(
        client,
        repo_root,
        proj,
        runnable,
        args,
        active_claimed_touches=active_touches,
        active_has_unscoped=active_unscoped,
    )
    print(
        safe_json(
            {
                "project": proj["name"],
                "dispatched": len(results),
                "results": results,
                "deferred_for_next_wave": deferred,
                "blocked": blocked,
                "skipped": skipped,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if all(r.get("status") in ("passed", "passed_local", "passed_branch_only") for r in results) else 1


def cmd_spec(args: argparse.Namespace) -> int:
    repo_root = get_repo_root(args.repo)
    client = LinearClient(resolve_linear_auth())
    repo_project = None if args.skip_project_check else resolve_project(client, repo_root, args.project)
    parent = client.get_issue(args.parent_issue)
    if not args.skip_project_check and repo_project and (parent.get("project") or {}).get("id") != repo_project["id"]:
        raise RuntimeError(f"Cross-repo guard: parent {parent['identifier']} does not belong to project '{repo_project['name']}'.")
    children = (parent.get("children") or {}).get("nodes", [])
    if not children:
        raise RuntimeError(f"{parent['identifier']} has no child issues.")
    scheduling_project = repo_project or parent.get("project")
    if not scheduling_project:
        raise RuntimeError("Parent Spec has no Linear project for Touches reconciliation.")
    started = client.list_project_issues(scheduling_project["id"], state_type="started")
    active_touches, active_unscoped = extract_active_touches(started)
    runnable, blocked, skipped = collect_runnable_tickets(client, children)
    if not runnable:
        print(
            safe_json(
                {"parent": parent["identifier"], "status": "no_unblocked_ready_children", "blocked": blocked, "skipped": skipped},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    results, deferred = run_batch_safely(
        client,
        repo_root,
        scheduling_project,
        runnable,
        args,
        active_claimed_touches=active_touches,
        active_has_unscoped=active_unscoped,
    )
    print(
        safe_json(
            {
                "parent": parent["identifier"],
                "wave_dispatched": len(results),
                "results": results,
                "deferred_for_next_wave": deferred,
                "remaining_blocked": blocked,
                "note": "Merge upstream Draft PRs and mark them Done in Linear, then rerun `agent-dispatch spec` for the next wave.",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if all(r.get("status") in ("passed", "passed_local", "passed_branch_only") for r in results) else 1


def cmd_review(args: argparse.Namespace) -> int:
    assert_required_rubrics_loaded()
    repo_root = get_repo_root(args.repo)
    base_ref = args.base or fetch_base_ref(repo_root, detect_default_branch(repo_root))

    base_sha = run_cmd(git_cmd("merge-base", base_ref, "HEAD"), cwd=repo_root, check=True, env_role="orchestrator").stdout.strip()
    candidate_sha = run_cmd(git_cmd("rev-parse", "HEAD"), cwd=repo_root, check=True, env_role="orchestrator").stdout.strip()
    if run_cmd(git_cmd("status", "--porcelain"), cwd=repo_root, env_role="orchestrator").stdout.strip():
        raise RuntimeError("Working tree has uncommitted changes; commit or stash before running SHA-locked `review`.")
    if not run_cmd(git_cmd("diff", "-z", "--name-only", f"{base_sha}..{candidate_sha}", attr_source=base_sha), cwd=repo_root, check=True, env_role="orchestrator").stdout.strip("\x00"):
        raise RuntimeError(f"Empty diff between {base_sha[:8]} and {candidate_sha[:8]}.")

    reviewer = parse_harness_spec(args.reviewer or "agy:neon")
    run_dir = _cache_root() / "reviews" / f"{repo_cache_key(repo_root)}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    auth_brief: Path | None = None
    auth_brief_sha256: str | None = None
    if args.issue:
        client = LinearClient(resolve_linear_auth())
        issue = client.get_issue(args.issue)
        auth_brief_content = render_issue_snapshot(issue)
        auth_brief = run_dir / "brief.md"
        atomic_write_text(auth_brief, auth_brief_content)
        auth_brief_sha256 = sha256_file(auth_brief)

    std_dir = run_dir / "baseline-standards"
    _, std_hashes = snapshot_baseline_standards(repo_root, base_sha, std_dir)
    sessions: list[dict[str, Any]] = []
    res = run_two_axis_review(
        reviewer,
        repo_root,
        run_dir,
        base_sha,
        candidate_sha,
        auth_brief,
        auth_brief_sha256,
        std_dir,
        std_hashes,
        round_num=1,
        trace_sink=sessions,
        review_timeout=args.review_timeout,
        block_added_secrets=True,
    )
    print(
        safe_json(
            {
                "reviewer": reviewer.label,
                "base_sha": base_sha,
                "candidate_sha": candidate_sha,
                "run_dir": str(run_dir),
                "sessions": sessions,
                **res,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if res["passed"] else 1
