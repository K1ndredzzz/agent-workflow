"""Recover interrupted pipeline runs and check for external changes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .git_ops import check_remote_base, get_remote_branch_sha, snapshot_local_git_config
from .linear import LinearClient
from .preflight import (
    PreparedWorktree as PreparedWorktree,
    RepositoryPreflight as RepositoryPreflight,
    RunOptions as RunOptions,
    TicketRun as TicketRun,
    inspect_repository as inspect_repository,
    prepare_ticket as prepare_ticket,
    stash_dirty_worktree_for_safe_rerun as stash_dirty_worktree_for_safe_rerun,
)
from .process import git_cmd, is_shutdown_requested, log, run_cmd
from .publish import claim_metadata_matches
from .secrets import redact_secrets


def _changed_git_file(
    worktree: Path, expected_git_cfg: dict[str, str], *, allow_gitattributes_edit: bool,
) -> str | None:
    current = snapshot_local_git_config(worktree)
    if current["local_config_sha"] != expected_git_cfg.get("local_config_sha"):
        return ".git/config"
    if not allow_gitattributes_edit and current["gitattributes_sha"] != expected_git_cfg.get("gitattributes_sha"):
        return ".gitattributes"
    return None


def assert_local_git_config_intact(
    worktree: Path, expected_git_cfg: dict[str, str], context: str, *, allow_gitattributes_edit: bool = False,
) -> None:
    changed = _changed_git_file(worktree, expected_git_cfg, allow_gitattributes_edit=allow_gitattributes_edit)
    if changed == ".git/config":
        raise RuntimeError(f"Unauthorized git config tampering {context} (.git/config modified).")
    if changed == ".gitattributes":
        raise RuntimeError(f"Unauthorized .gitattributes tampering {context}.")


def assert_no_external_tampering(
    client: LinearClient,
    repo_root: Path,
    worktree: Path,
    branch: str,
    base_branch: str,
    issue_uuid: str,
    expected_branch_sha: str,
    expected_base_sha: str,
    expected_git_cfg: dict[str, str],
    expected_ownership: dict[str, str],
    phase: str,
    *,
    allow_gitattributes_edit: bool = False,
) -> None:
    """Check this branch, remote base ancestry, Git configuration, and Linear ownership."""
    if expected_git_cfg:
        assert_local_git_config_intact(
            worktree, expected_git_cfg, f"detected during {phase}", allow_gitattributes_edit=allow_gitattributes_edit,
        )
    cur_branch_sha = get_remote_branch_sha(repo_root, branch)
    if cur_branch_sha != expected_branch_sha:
        raise RuntimeError(
            f"Unauthorized external mutation during {phase}: origin/{branch} changed "
            f"from {expected_branch_sha[:8] or 'none'} to {cur_branch_sha[:8] or 'none'}."
        )
    check_remote_base(repo_root, base_branch, expected_base_sha, phase)
    if expected_ownership:
        cur = client.get_issue_ownership_snapshot(issue_uuid)
        if cur.get("state_type") in ("completed", "canceled"):
            raise RuntimeError(
                f"Human terminal state conflict during {phase}: Linear issue was moved to '{cur.get('state_name')}'."
            )
        if expected_ownership.get("state_id") and cur.get("state_id") != expected_ownership.get("state_id"):
            raise RuntimeError(
                f"External Linear state mutation during {phase}: state changed from "
                f"'{expected_ownership.get('state_name')}' to '{cur.get('state_name')}'."
            )
        if cur.get("assignee_id") != expected_ownership.get("assignee_id"):
            raise RuntimeError(f"External Linear assignee mutation during {phase}: assignee changed mid-run.")
        if expected_ownership.get("description_sha") and cur.get("description_sha") != expected_ownership.get("description_sha"):
            raise RuntimeError(f"External Linear description mutation during {phase}: acceptance criteria changed mid-run.")
        if expected_ownership.get("updated_at") and cur.get("updated_at") and cur.get("updated_at") != expected_ownership.get("updated_at"):
            log(f"Note: benign Linear metadata update detected during {phase} (state/assignee/description unchanged).")


def record_pipeline_error(run: TicketRun, exc: BaseException) -> None:
    run.failure_reason = f"Unhandled orchestrator error ({type(exc).__name__}): {exc}"
    state = run.state
    checkout = run.checkout
    if not run.publish_started and (state["stage"] != "claim_pending" or not run.claim_attempted):
        state["stage"] = "error"
        if (checkout.path / ".git").exists() and run.pre_git_cfg and not is_shutdown_requested():
            changed = _changed_git_file(
                checkout.path, run.pre_git_cfg, allow_gitattributes_edit=run.allow_root_gitattributes,
            )
            if changed is None and run_cmd(
                git_cmd("branch", "--show-current"), cwd=checkout.path, env_role="orchestrator",
            ).stdout.strip() == checkout.branch:
                run.stash("error")
    state["error"] = redact_secrets(str(exc))
    state["sessions"] = run.sessions
    try:
        run.save()
    except Exception:
        pass


def confirm_remote_push(run: TicketRun) -> None:
    """Record a push even if the subprocess was interrupted before returning."""
    state = run.state
    if get_remote_branch_sha(run.repo.root, run.checkout.branch, ignore_shutdown=True) == state.get("candidate_sha"):
        state["pushed"] = True
        state["push_attempted"] = True
        if state.get("stage") in ("verified_and_reviewed", "pushing"):
            state["stage"] = "pushed"
        run.save()


def recover_interrupted_run(run: TicketRun) -> None:
    if run.terminal_ok or run.options.no_push or not (run.moved_in_progress or run.claim_attempted):
        return
    state = run.state
    if not state.get("pushed") and (run.publish_started or state.get("push_attempted")):
        # Persist the remote push fact before attempting any Linear I/O.
        try:
            confirm_remote_push(run)
        except BaseException as exc:
            log(f"Warning: remote push probe failed during cleanup for {run.identifier}: {redact_secrets(str(exc))}")

    client, issue_id = run.client, run.issue["id"]
    rollback_reverted = False
    human_conflict = False
    try:
        live_issue = client.get_issue(issue_id)
        live_state = live_issue.get("state") or {}
        live_state_type = str(live_state.get("type") or "")
        live_state_id = str(live_state.get("id") or "")
        # A lost Claim acknowledgment requires rollback only if Linear moved the issue.
        if not run.moved_in_progress and live_state_id == state["pre_claim_state_id"]:
            return
        human_took_over = not claim_metadata_matches(live_issue, state) or live_state_type in ("completed", "canceled") or (
            bool(run.claimed_state_id) and live_state_id != run.claimed_state_id
        )

        if run.publish_started:
            _report_publish_interruption(run, live_issue, live_state_type)
        elif human_took_over:
            human_conflict = True
            client.add_comment(
                live_issue["id"], "\n".join([
                    f"### [agent-dispatch] Halted (Preserved Current State `{live_state.get('name', 'unknown')}`)",
                    f"- **Worktree**: `{run.checkout.path}` (`{run.checkout.branch}`)",
                    f"- **Run Logs**: `{run.run_dir}`",
                    f"- **Reason**:\n{redact_secrets(run.failure_reason)[:2500]}",
                ]),
            )
        else:
            # Label first so an interrupted rollback cannot leave a ready ticket eligible again.
            client.ensure_label_and_add(live_issue, "dispatch:failed")
            live_issue = client.get_issue(issue_id)
            if live_issue["state"]["id"] != run.claimed_state_id or not claim_metadata_matches(live_issue, state):
                human_conflict = True
                raise RuntimeError("Human Linear ownership changed during rollback.")
            if state["pre_claim_state_id"]:
                client.update_issue_state_by_id(live_issue, state["pre_claim_state_id"], label_hint=state["pre_claim_state_name"])
            else:
                client.update_issue_state(live_issue, "Todo")
            rollback_reverted = True
            _report_rollback(run, live_issue)
    except BaseException as exc:
        log(f"Warning: failed Linear cleanup for {run.identifier}: {redact_secrets(str(exc))}")
        if not run.publish_started and run.moved_in_progress and not human_conflict:
            state["rollback_from_stage"] = state.get("rollback_from_stage") or state.get("stage")
            state["stage"] = "rollback_pending"
            state["rollback_state_reverted"] = rollback_reverted
            state["failure_reason"] = redact_secrets(run.failure_reason)[:2500]
            state["rollback_error"] = redact_secrets(str(exc))[:1000]
            state["sessions"] = run.sessions
            try:
                run.save()
            except Exception as save_exc:
                log(f"Warning: failed persisting rollback_pending checkpoint for {run.identifier}: {redact_secrets(str(save_exc))}")


def _report_publish_interruption(run: TicketRun, live_issue: dict[str, Any], live_state_type: str) -> None:
    state = run.state
    if live_state_type not in ("completed", "canceled") and claim_metadata_matches(live_issue, state):
        run.client.ensure_label_and_add(live_issue, "dispatch:publish-incomplete")
    run.client.add_comment(
        live_issue["id"], "\n".join([
            f"### [agent-dispatch] Publish Interrupted at `{state.get('stage')}`",
            f"- **Branch / SHA**: `{run.checkout.branch}` (`{state.get('candidate_sha', '')[:8]}`)",
            f"- **Remote Push Status**: {'confirmed' if state.get('pushed') else 'attempted (verify via resume)'}",
            f"- **PR**: {state.get('pr_url') or 'not confirmed yet'}",
            f"- **Resume**: `agent-dispatch resume {run.identifier}`",
            f"- **Run Logs**: `{run.run_dir}`",
            f"- **Reason**:\n{redact_secrets(run.failure_reason)[:2500]}",
        ]),
    )


def _report_rollback(run: TicketRun, live_issue: dict[str, Any]) -> None:
    comment_parts = [
        f"<!-- agent-dispatch-rollback:{run.run_id} -->",
        f"### [agent-dispatch] Halted -> Reverted to `{run.state['pre_claim_state_name']}` (`dispatch:failed`)",
        f"- **Worktree (local, NOT pushed by this run)**: `{run.checkout.path}` (`{run.checkout.branch}`)",
        f"- **Run Logs**: `{run.run_dir}`",
        f"- **Reason**:\n{redact_secrets(run.failure_reason)[:2500]}",
    ]
    if run.sessions:
        comment_parts += ["", "**Sessions:**"]
        comment_parts.extend(
            f"- `{s.get('role')}` ({s.get('harness')}): `{s.get('resume_cmd') or s.get('conversation_link') or s.get('stdout_log')}`"
            for s in run.sessions
        )
    run.client.add_comment(live_issue["id"], "\n".join(comment_parts))
