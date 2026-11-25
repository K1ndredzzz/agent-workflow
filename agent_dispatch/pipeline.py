"""Execute an isolated ticket from preflight through Draft PR Delivery."""

from __future__ import annotations

import os
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import recovery
from .brief import assert_sha256_intact
from .config import WORKER_SCHEMA, assert_required_rubrics_loaded
from .gates import (
    check_protected_paths,
    check_test_integrity_diff,
    detect_verify_command,
    orchestrator_commit,
    run_verify_gate,
    scan_range_for_secrets,
)
from .git_ops import (
    check_remote_base,
    get_remote_branch_sha,
    snapshot_local_git_config,
    ticket_lock_path,
    verify_worktree_branch_integrity,
)
from .harness import HarnessSpec, invoke_harness
from .linear import LinearClient
from .process import check_shutdown, file_lock, git_cmd, log, run_cmd
from .publish import (
    build_pr_body,
    fetch_open_pr_feedback,
    fetch_open_pr_for_branch,
    reconcile_publish,
    validate_open_draft_pr,
)
from .recovery import RunOptions, TicketRun
from .review import build_worker_prompt, run_two_axis_review
from .secrets import redact_secrets


@dataclass
class _Rework:
    verify_cmd: str | None
    pr_feedback: str | None
    feedback: str | None
    prior_findings: dict[str, list[str]] | None = None
    discovered_seen: set[str] = field(default_factory=set)
    discovered: list[str] = field(default_factory=list)
    last_verify: dict[str, Any] = field(default_factory=dict)
    last_review: dict[str, Any] = field(default_factory=dict)


def execute_ticket(
    client: LinearClient,
    repo_root: Path,
    repo_project: dict[str, Any] | None,
    identifier: str,
    worker: HarnessSpec,
    reviewer: HarnessSpec,
    *,
    verify_cmd_override: str | None,
    setup_cmd_override: str | None,
    max_rework: int,
    verify_timeout: int,
    setup_timeout: int,
    worker_timeout: int = 1800,
    review_timeout: int = 900,
    no_push: bool,
    allow_no_verify: bool,
    allow_gate_edits: bool,
    allow_test_edits: bool,
    allow_dirty_docs: bool,
    allow_branch_only: bool = False,
    skip_baseline: bool,
    skip_project_check: bool,
    force_rerun: bool,
    fresh: bool,
    auto_claimed: bool = False,
    reserved_touches: set[str] | None = None,
) -> dict[str, Any]:
    if max_rework < 0 or max_rework > 5:
        raise ValueError("--max-rework must be between 0 and 5")
    assert_required_rubrics_loaded()
    check_shutdown()
    options = RunOptions(
        verify_cmd_override=verify_cmd_override,
        setup_cmd_override=setup_cmd_override,
        max_rework=max_rework,
        verify_timeout=verify_timeout,
        setup_timeout=setup_timeout,
        worker_timeout=worker_timeout,
        review_timeout=review_timeout,
        no_push=no_push,
        allow_no_verify=allow_no_verify,
        allow_gate_edits=allow_gate_edits,
        allow_test_edits=allow_test_edits,
        allow_dirty_docs=allow_dirty_docs,
        allow_branch_only=allow_branch_only,
        skip_baseline=skip_baseline,
        skip_project_check=skip_project_check,
        force_rerun=force_rerun,
        fresh=fresh,
        auto_claimed=auto_claimed,
        reserved_touches=reserved_touches,
    )
    repo = recovery.inspect_repository(repo_root, options)
    # Resolve the canonical UUID before taking the cross-clone ticket lock.
    issue = client.get_issue(identifier)
    lock_path = ticket_lock_path(repo_root, issue["id"])
    with file_lock(lock_path, blocking=False), ExitStack() as cleanup:
        run = recovery.prepare_ticket(client, repo, repo_project, issue, worker, reviewer, options, cleanup)
        if isinstance(run, dict):
            return run
        try:
            _claim_ticket(run)
            pr_feedback = _get_pr_feedback(run)
            rework = _Rework(run.checkout.verify_cmd, pr_feedback, pr_feedback)
            return _run_rework_rounds(run, rework)
        except BaseException as exc:
            recovery.record_pipeline_error(run, exc)
            raise
        finally:
            recovery.recover_interrupted_run(run)


def _claim_ticket(run: TicketRun) -> None:
    state = run.state
    checkout = run.checkout
    issue = run.issue
    if not run.options.no_push:
        check_remote_base(run.repo.root, run.repo.base_branch, checkout.target_sha, "preflight")
        if get_remote_branch_sha(run.repo.root, checkout.branch) != checkout.pre_branch_sha:
            raise RuntimeError(f"Remote origin/{checkout.branch} changed during preflight.")
        # Persist the pending Claim before the mutation so a lost ACK is recoverable.
        state["stage"] = "claim_pending"
        run.save()
        before_claim = run.client.get_issue_ownership_snapshot(issue["id"])
        if (before_claim["state_id"] != state["pre_claim_state_id"]
                or before_claim["updated_at"] != str(issue.get("updatedAt") or "")
                or any(before_claim[key] != value for key, value in state["claimed_ownership"].items())):
            raise RuntimeError("Linear issue changed while preparing claim. Rerun with its current brief and state.")
        check_shutdown()
        run.claim_attempted = True
        claimed_state = run.client.update_issue_state(issue, "In Progress")
        run.claimed_state_id = str(claimed_state.get("id") or run.claimed_state_id)
        run.moved_in_progress = True
    state["stage"] = "in_progress"
    state["claimed_state_id"] = run.claimed_state_id
    if not run.options.no_push:
        run.expected_ownership = run.client.get_issue_ownership_snapshot(issue["id"])
        if (run.expected_ownership["state_id"] != run.claimed_state_id
                or any(run.expected_ownership[key] != value for key, value in state["claimed_ownership"].items())):
            raise RuntimeError("Linear ownership changed while claiming the reviewed brief.")
    run.save()
    run.pre_git_cfg = snapshot_local_git_config(checkout.path)


def _get_pr_feedback(run: TicketRun) -> str | None:
    checkout = run.checkout
    if run.options.no_push or not (checkout.gh_repo or os.environ.get("AGENT_DISPATCH_FORCE_GH") == "1"):
        return None
    preflight_pr = checkout.preflight_pr
    observed_pr = fetch_open_pr_for_branch(run.repo.root, checkout.gh_repo, checkout.branch)
    if preflight_pr and (not observed_pr or observed_pr["number"] != preflight_pr["number"]):
        raise RuntimeError(f"Open PR #{preflight_pr['number']} changed during preflight (human action preserved).")
    if observed_pr:
        validate_open_draft_pr(observed_pr, base_branch=run.repo.base_branch, context="pre-worker PR check")
        run.state["pr_number"] = observed_pr["number"]
        run.save()
        return fetch_open_pr_feedback(run.repo.root, checkout.gh_repo, observed_pr["number"])
    return None


def _check_tampering(run: TicketRun, phase: str) -> None:
    checkout = run.checkout
    recovery.assert_no_external_tampering(
        run.client, run.repo.root, checkout.path, checkout.branch, run.repo.base_branch, run.issue["id"],
        checkout.pre_branch_sha, checkout.target_sha, run.pre_git_cfg, run.expected_ownership, phase,
        allow_gitattributes_edit=run.allow_root_gitattributes,
    )


def _run_worker(run: TicketRun, rework: _Rework, attempt: int) -> dict[str, Any] | None:
    for path, digest in run.worker_artifacts.items():
        assert_sha256_intact(path, digest, path.name)
    prompt = build_worker_prompt(
        run.identifier,
        run.auth_brief_path,
        rework.verify_cmd,
        allow_gate_edits=run.options.allow_gate_edits,
        standard_paths=run.standard_paths,
        authorized_gate_patterns=run.state["gate_authorizations"]["brief_gate_patterns"],
        feedback=rework.feedback,
    )
    timeout = run.options.worker_timeout
    try:
        result = invoke_harness(
            run.worker,
            prompt,
            worktree=run.checkout.path,
            run_dir=run.run_dir,
            schema=WORKER_SCHEMA,
            schema_file=run.run_dir / "worker-schema.json",
            prompt_file=run.run_dir / f"prompt-worker-r{attempt}.md",
            out_file=run.run_dir / f"worker-r{attempt}.json",
            read_only=False,
            session_label=f"{run.identifier}-worker-r{attempt}",
            trace_sink=run.sessions,
            attach_files=[run.auth_brief_path, *run.standard_paths],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if not run.options.no_push:
            _check_tampering(run, f"worker timeout attempt {attempt}")
        rework.feedback = (
            f"Previous worker attempt timed out after {timeout}s. "
            "Inspect current partial edits in the worktree, keep changes minimal, verify, and return JSON promptly."
        )
        log(f"{run.identifier} worker timed out on attempt {attempt} ({timeout}s); routing to rework.")
        return None

    if not run.options.no_push:
        _check_tampering(run, f"worker attempt {attempt}")
    else:
        # Local runs still validate Git configuration and root attributes.
        cur_cfg = snapshot_local_git_config(run.checkout.path)
        if cur_cfg["local_config_sha"] != run.pre_git_cfg.get("local_config_sha"):
            raise RuntimeError(f"Unauthorized git config tampering during worker attempt {attempt} (.git/config modified).")
        if not run.allow_root_gitattributes and cur_cfg["gitattributes_sha"] != run.pre_git_cfg.get("gitattributes_sha"):
            raise RuntimeError(f"Unauthorized .gitattributes tampering during worker attempt {attempt}.")

    for raw_item in result.get("discovered_issues") or []:
        norm = raw_item.strip().lower()
        if norm and norm not in rework.discovered_seen:
            rework.discovered_seen.add(norm)
            rework.discovered.append(f"[attempt {attempt}] {redact_secrets(raw_item.strip())}")
    return result


def _finish_failed_run(
    run: TicketRun, stage: str, failure_reason: str, **details: Any,
) -> dict[str, Any]:
    run.failure_reason = failure_reason
    run.state.update(stage=stage, sessions=run.sessions)
    run.save()
    return {
        "issue": run.identifier,
        "status": stage,
        "branch": run.checkout.branch,
        "worktree": str(run.checkout.path),
        "run_dir": str(run.run_dir),
        "sessions": run.sessions,
        **details,
    }


def _prepare_candidate(run: TicketRun, rework: _Rework, attempt: int) -> str | dict[str, Any] | None:
    checkout = run.checkout
    verify_worktree_branch_integrity(checkout.path, run.repo.common_dir, checkout.branch, checkout.base_sha)
    # Keep setup paths excluded across rework, even if verification writes to them.
    candidate_sha, staged_secret_hits = orchestrator_commit(
        checkout.path, run.identifier, run.issue["title"], attempt, setup_paths=tuple(checkout.setup_files),
        attr_source=checkout.base_sha,
    )
    if staged_secret_hits:
        if any("ticket title" in hit for hit in staged_secret_hits):
            run.stash("title-secret")
            reason = "Secret detected in ticket title / commit message:\n" + "\n".join(f"- {hit}" for hit in staged_secret_hits)
            return _finish_failed_run(run, "secret_in_metadata", reason, error=reason)
        rework.feedback = "Secret scan blocked staging (remove secrets/credential files and retry):\n" + "\n".join(
            f"- {hit}" for hit in staged_secret_hits
        )
        log(f"{run.identifier} staged secret scan triggered rework on attempt {attempt}.")
        return None

    diff_nonempty = run_cmd(
        git_cmd("diff", "-z", "--name-only", f"{checkout.base_sha}..{candidate_sha}", attr_source=checkout.base_sha),
        cwd=checkout.path, check=True, env_role="orchestrator",
    ).stdout.strip("\x00")
    if not diff_nonempty:
        rework.feedback = "Net diff against base commit is empty. Implement the ticket acceptance criteria."
        return None

    range_secret_hits = scan_range_for_secrets(checkout.path, checkout.base_sha, candidate_sha)
    if range_secret_hits:
        reason = "Secret detected in commit history:\n" + "\n".join(f"- {hit}" for hit in range_secret_hits)
        return _finish_failed_run(run, "secret_in_history", reason, error=reason)
    return candidate_sha


def _verify_candidate(run: TicketRun, rework: _Rework, attempt: int, candidate_sha: str) -> bool:
    worktree, base_sha = run.checkout.path, run.checkout.base_sha
    gate_authorizations = run.state["gate_authorizations"]
    protected_violations = check_protected_paths(
        worktree, base_sha, candidate_sha,
        allowed_patterns=gate_authorizations["brief_gate_patterns"], allow_all=run.options.allow_gate_edits,
    )
    if protected_violations:
        rework.feedback = (
            f"Protected policy/gate files were modified. Restore them to base commit `{base_sha}` "
            f"(e.g., `git checkout {base_sha} -- <path>`):\n"
            + "\n".join(f"- {violation}" for violation in protected_violations)
        )
        log(f"{run.identifier} protected-paths gate FAILED on attempt {attempt}.")
        return False

    test_violations = check_test_integrity_diff(
        worktree, base_sha, candidate_sha,
        allowed_patterns=gate_authorizations["brief_test_patterns"], allow_all=run.options.allow_test_edits,
    )
    if test_violations:
        rework.feedback = "Deterministic test-integrity check FAILED (restore skipped/deleted tests):\n" + "\n".join(
            f"- {violation}" for violation in test_violations
        )
        log(f"{run.identifier} deterministic test-integrity gate FAILED on attempt {attempt}.")
        return False

    # The candidate can add a suite or change an authorized verification target.
    rework.verify_cmd = detect_verify_command(worktree, run.options.verify_cmd_override)
    result = run_verify_gate(
        worktree,
        rework.verify_cmd,
        allow_no_verify=run.options.allow_no_verify,
        log_file=run.run_dir / f"verify-r{attempt}.log",
        repo_key=run.repo.key,
        timeout=run.options.verify_timeout,
    )
    rework.last_verify = result
    if not result["passed"]:
        rework.feedback = (
            f"Deterministic verification failed (`{result['command']}`, exit {result.get('returncode')}):\n"
            f"```\n{result['output'][-2500:]}\n```"
        )
        log(f"{run.identifier} verify FAILED on attempt {attempt}.")
        return False
    return True


def _review_candidate(run: TicketRun, rework: _Rework, attempt: int, candidate_sha: str) -> dict[str, Any]:
    if not run.options.no_push:
        _check_tampering(run, f"verify attempt {attempt}")
    result = run_two_axis_review(
        run.reviewer,
        run.checkout.path,
        run.run_dir,
        run.checkout.base_sha,
        candidate_sha,
        run.auth_brief_path,
        run.auth_brief_sha256,
        run.baseline_std_dir,
        run.baseline_std_hashes,
        attempt,
        prior_findings=rework.prior_findings,
        pr_feedback=rework.pr_feedback,
        trace_sink=run.sessions,
        review_timeout=run.options.review_timeout,
        setup_files=run.checkout.setup_files,
    )
    if not run.options.no_push:
        _check_tampering(run, f"review attempt {attempt}")
    return result


def _record_review_feedback(rework: _Rework) -> None:
    review = rework.last_review
    rework.prior_findings = {
        "standards": list(review["standards"].get("hard_findings") or []),
        "spec": list(review["spec"].get("hard_findings") or []),
    }
    lines: list[str] = []
    if rework.pr_feedback:
        lines += ["### Open PR Feedback (must stay addressed):", rework.pr_feedback, ""]
    for axis_name in ("standards", "spec"):
        axis = review[axis_name]
        if axis.get("hard_findings") or axis.get("test_integrity_violation") or axis.get("verdict") == "fail":
            lines.append(f"### {axis_name.upper()} Findings:")
            if axis.get("test_integrity_violation"):
                lines.append("- [REDLINE] Test integrity violation detected. Restore assertion strength.")
            lines.extend(f"- {finding}" for finding in axis.get("hard_findings", []))
            if not axis.get("hard_findings") and axis.get("summary"):
                lines.append(f"- {axis['summary']}")
    rework.feedback = "\n".join(lines) or "Review failed; address all review feedback."


def _run_rework_rounds(run: TicketRun, rework: _Rework) -> dict[str, Any]:
    max_attempts = 1 + run.options.max_rework
    for attempt in range(1, max_attempts + 1):
        check_shutdown()
        log(f"=== {run.identifier} Attempt {attempt}/{max_attempts} | worker={run.worker.label} ===")
        worker_result = _run_worker(run, rework, attempt)
        if worker_result is None:
            continue
        if worker_result["status"] == "blocked":
            run.stash("blocked")
            blockers = redact_secrets(worker_result["blockers"])
            return _finish_failed_run(
                run, "worker_blocked", f"Worker reported blocked: {blockers}", attempt=attempt, blockers=blockers,
                summary=redact_secrets(worker_result["summary"]), discovered_issues=rework.discovered,
            )

        candidate = _prepare_candidate(run, rework, attempt)
        if isinstance(candidate, dict):
            return candidate
        if candidate is None or not _verify_candidate(run, rework, attempt, candidate):
            continue
        rework.last_review = _review_candidate(run, rework, attempt, candidate)
        if rework.last_review["passed"]:
            _checkpoint_reviewed_candidate(run, rework, attempt, candidate, worker_result)
            return _deliver_candidate(run)
        _record_review_feedback(rework)
        log(f"{run.identifier} review FAILED on attempt {attempt}.")

    run.stash("max-rework")
    reason = f"Exhausted {max_attempts} attempts ({run.options.max_rework} rework rounds). Last feedback:\n{rework.feedback or 'unknown'}"
    return _finish_failed_run(
        run, "failed_max_rework", reason, attempts=max_attempts, worker=run.worker.label, reviewer=run.reviewer.label,
        last_verify=rework.last_verify, last_review=rework.last_review, discovered_issues=rework.discovered,
    )


def _checkpoint_reviewed_candidate(
    run: TicketRun, rework: _Rework, attempt: int, candidate_sha: str, worker_result: dict[str, Any],
) -> None:
    safe_summary = redact_secrets(worker_result["summary"])
    safe_tests_run = [redact_secrets(test) for test in (worker_result.get("tests_run") or [])]
    pr_body = build_pr_body(
        run.issue,
        candidate_sha,
        run.worker,
        run.reviewer,
        safe_summary,
        safe_tests_run,
        rework.last_verify,
        rework.last_review,
        rework.discovered,
        gate_authorizations=run.state["gate_authorizations"],
    )
    run.state.update(
        pre_push_sha=run.checkout.pre_branch_sha,
        stage="verified_and_reviewed",
        attempt=attempt,
        max_attempts=1 + run.options.max_rework,
        candidate_sha=candidate_sha,
        worker_summary=safe_summary,
        tests_run=safe_tests_run,
        verify={
            "command": redact_secrets(str(rework.last_verify.get("command") or "")),
            "ran": rework.last_verify.get("ran"),
            "passed": True,
        },
        review=rework.last_review,
        discovered_issues=rework.discovered,
        sessions=run.sessions,
        pr_body=pr_body,
        push_attempted=False,
        pushed=False,
        pr_url="",
        linear_state_synced=False,
        linear_comment_posted=False,
    )
    run.save()


def _deliver_candidate(run: TicketRun) -> dict[str, Any]:
    state = run.state
    if run.options.no_push:
        run.terminal_ok = True
        state["stage"] = "passed_local"
        run.save()
        status = "passed_local"
        delivery = {}
    else:
        run.publish_started = True
        try:
            reconcile_publish(
                run.client, run.issue, run.repo.root, run.run_dir, run.latest_file, state,
                allow_branch_only=run.options.allow_branch_only,
            )
        except Exception as exc:
            return _publish_incomplete(run, exc)

        run.terminal_ok = True
        status = "passed" if state.get("stage") == "completed" else "passed_branch_only"
        delivery = {"delivery_mode": state.get("delivery_mode", "draft_pr"), "pr_url": state.get("pr_url", "")}

    return {
        "issue": run.identifier,
        "status": status,
        "attempt": state["attempt"],
        "branch": run.checkout.branch,
        "commit": state["candidate_sha"],
        "worktree": str(run.checkout.path),
        "run_dir": str(run.run_dir),
        **delivery,
        "worker": run.worker.label,
        "reviewer": run.reviewer.label,
        "verify": state["verify"],
        "review": state["review"],
        "discovered_issues": state["discovered_issues"],
        "sessions": run.sessions,
    }


def _publish_incomplete(run: TicketRun, exc: Exception) -> dict[str, Any]:
    state = run.state
    if not state.get("pushed") and state.get("push_attempted"):
        try:
            recovery.confirm_remote_push(run)
        except BaseException:
            pass
    run.failure_reason = (
        f"Publish interrupted at stage '{state['stage']}' (fix & run `agent-dispatch resume {run.identifier}`): {exc}"
    )
    return {
        "issue": run.identifier,
        "status": "publish_incomplete",
        "stage": state["stage"],
        "push_attempted": bool(state.get("push_attempted")),
        "pushed": bool(state.get("pushed")),
        "pr_url": state.get("pr_url", ""),
        "error": redact_secrets(str(exc)),
        "commit": state["candidate_sha"],
        "branch": run.checkout.branch,
        "run_dir": str(run.run_dir),
        "resume_cmd": f"agent-dispatch resume {run.identifier}",
        "sessions": run.sessions,
    }
