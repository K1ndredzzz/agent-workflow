"""Reconcile Draft PR Delivery and persist its recovery checkpoints."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import TRUSTED_PR_AUTHOR_ASSOCIATIONS
from .git_ops import canonical_repo_identity, check_remote_base, classify_origin_remote, get_remote_branch_sha
from .harness import HarnessSpec
from .linear import LinearClient
from .process import (
    atomic_write_text,
    check_shutdown,
    git_cmd,
    interruptible_sleep,
    is_shutdown_requested,
    log,
    run_cmd,
    sha256_text,
)
from .review import format_review_markdown
from .secrets import redact_secrets, safe_json, scan_text_for_secrets


def build_pr_body(
    issue: dict[str, Any],
    candidate_sha: str,
    worker: HarnessSpec,
    reviewer: HarnessSpec,
    worker_summary: str,
    tests_run: list[str],
    verify_res: dict[str, Any],
    review_res: dict[str, Any],
    discovered_issues: list[str],
    *,
    gate_authorizations: dict[str, Any] | None = None,
) -> str:
    verify_cmd_safe = redact_secrets(str(verify_res.get("command") or ""))
    verify_line = f"`{verify_cmd_safe}` -> **PASS**" if verify_res.get("ran") else "Bypassed (`--allow-no-verify`)"
    tests_safe = ", ".join(redact_secrets(t) for t in tests_run) or "N/A"
    auth_lines: list[str] = []
    if gate_authorizations:
        if gate_authorizations.get("cli_allow_gate_edits"):
            auth_lines.append("- CLI override: `--allow-gate-edits` (all protected gate paths)")
        if gate_authorizations.get("cli_allow_test_edits"):
            auth_lines.append("- CLI override: `--allow-test-edits` (all test-integrity checks)")
        if gate_authorizations.get("brief_gate_patterns"):
            auth_lines.append(f"- Brief-authorized gate paths: {', '.join(f'`{p}`' for p in gate_authorizations['brief_gate_patterns'])}")
        if gate_authorizations.get("brief_test_patterns"):
            auth_lines.append(f"- Brief-authorized test paths: {', '.join(f'`{p}`' for p in gate_authorizations['brief_test_patterns'])}")
    lines = [
        "## Linear Issue",
        f"- [{issue['identifier']}: {issue['title']}]({issue.get('url', '')})",
        "",
        "## Implementation Summary",
        f"- **Candidate SHA**: `{candidate_sha}`",
        f"- **Worker**: `{worker.label}`",
        f"- **Reviewer**: `{reviewer.label}`",
        f"- **Summary**: {redact_secrets(worker_summary)}",
        "",
        "## Verification",
        f"- {verify_line}",
        f"- Worker tests run: {tests_safe}",
    ]
    if auth_lines:
        lines += ["", "## Active Gate/Test Authorizations", *auth_lines]
    lines += [
        "",
        "## Two-Axis Code Review (Unmerged)",
        format_review_markdown(review_res),
        "",
        "## Side Issues Discovered (Untouched)",
    ]
    lines.extend([f"- {redact_secrets(d)}" for d in discovered_issues] or ["- None"])
    return redact_secrets("\n".join(lines) + "\n")


PR_FIELDS = "url,number,state,isDraft,headRefOid,headRefName,baseRefName,isCrossRepository"


def fetch_open_prs_for_branch(worktree: Path, gh_repo: str | None, branch: str, *, state: str = "open") -> list[dict[str, Any]]:
    cmd = ["gh", "pr", "list", "--head", branch, "--state", state, "--limit", "100", "--json", PR_FIELDS]
    if gh_repo:
        cmd += ["--repo", gh_repo]
    res = run_cmd(cmd, cwd=worktree, env_role="orchestrator", check=True, cap_output=False)
    prs = json.loads(res.stdout)
    if not isinstance(prs, list) or len(prs) >= 100:
        raise RuntimeError(f"Incomplete GitHub PR lookup for {branch}.")
    return [pr for pr in prs if pr["isCrossRepository"] is False and pr["headRefName"] == branch]


def fetch_open_pr_for_branch(worktree: Path, gh_repo: str | None, branch: str) -> dict[str, Any] | None:
    prs = fetch_open_prs_for_branch(worktree, gh_repo, branch)
    if len(prs) > 1:
        nums = [p.get("number") for p in prs]
        raise RuntimeError(
            f"Multiple open PRs share head branch '{branch}' (#{nums}); refusing ambiguous push/update."
        )
    return prs[0] if prs else None


def fetch_open_pr_feedback(worktree: Path, gh_repo: str | None, pr_number: int) -> str | None:
    """Fetch PR comments, reviews, and paginated inline review threads ONLY from explicitly trusted authors."""
    cmd = ["gh", "pr", "view", str(pr_number), "--json", "comments,reviews"]
    if gh_repo:
        cmd += ["--repo", gh_repo]
    res = run_cmd(cmd, cwd=worktree, env_role="orchestrator")
    if res.returncode != 0:
        raise RuntimeError(f"gh pr view failed ({res.returncode}): {redact_secrets(res.stderr)}")
    data = json.loads(res.stdout or "{}")
    notes: list[str] = []
    for r in data.get("reviews") or []:
        assoc = (r.get("authorAssociation") or "").upper()
        if assoc not in TRUSTED_PR_AUTHOR_ASSOCIATIONS:
            continue
        body = (r.get("body") or "").strip()
        if body and r.get("state") in ("CHANGES_REQUESTED", "COMMENTED"):
            author = (r.get("author") or {}).get("login", "reviewer")
            notes.append(f"- [PR Review by @{author} ({r.get('state')})]: {redact_secrets(body)}")
    for c in data.get("comments") or []:
        assoc = (c.get("authorAssociation") or "").upper()
        if assoc not in TRUSTED_PR_AUTHOR_ASSOCIATIONS:
            continue
        body = (c.get("body") or "").strip()
        if body and "[agent-dispatch]" not in body:
            author = (c.get("author") or {}).get("login", "user")
            notes.append(f"- [PR Comment by @{author}]: {redact_secrets(body)}")

    # Paginated inline code review comments via REST API
    if gh_repo:
        inline = run_cmd(
            ["gh", "api", "--paginate", "--slurp", f"repos/{gh_repo}/pulls/{pr_number}/comments"],
            cwd=worktree,
            env_role="orchestrator",
            cap_output=False,
        )
        if inline.returncode != 0:
            log(f"Warning: failed fetching inline PR comments for #{pr_number}: {redact_secrets(inline.stderr)[:300]}")
        elif inline.stdout.strip():
            try:
                parsed = json.loads(inline.stdout)
                # --slurp produces list of pages (list[list[dict]]) or list[dict]
                items: list[dict[str, Any]] = []
                if isinstance(parsed, list):
                    for elem in parsed:
                        if isinstance(elem, list):
                            items.extend(x for x in elem if isinstance(x, dict))
                        elif isinstance(elem, dict):
                            items.append(elem)
                for ic in items:
                    assoc = (ic.get("author_association") or "").upper()
                    if assoc not in TRUSTED_PR_AUTHOR_ASSOCIATIONS:
                        continue
                    body = (ic.get("body") or "").strip()
                    if body:
                        author = (ic.get("user") or {}).get("login", "reviewer")
                        path = ic.get("path", "")
                        line = ic.get("line") or ic.get("original_line") or ""
                        notes.append(f"- [Inline PR Comment by @{author} on `{path}:{line}`]: {redact_secrets(body)}")
            except json.JSONDecodeError as exc:
                log(f"Warning: could not parse inline PR comments JSON for #{pr_number}: {exc}")
    return redact_secrets("\n".join(notes)) if notes else None


PUBLISH_STAGES = ("verified_and_reviewed", "pushing", "pushed", "pr_ready", "linear_state_synced")
COMPLETED_STAGES = ("completed", "completed_branch_only")


def save_checkpoint(run_dir: Path, latest_file: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload = safe_json(state, indent=2, ensure_ascii=False)
    atomic_write_text(run_dir / "state.json", payload)
    atomic_write_text(latest_file, safe_json({"run_dir": str(run_dir), **state}, indent=2, ensure_ascii=False))


def validate_open_draft_pr(pr: dict[str, Any] | None, *, base_branch: str, candidate_sha: str | None = None, context: str = "") -> dict[str, Any]:
    if not pr:
        raise RuntimeError(f"Expected open Draft PR ({context}), but GitHub returned no open PR.")
    if pr.get("state") != "OPEN":
        raise RuntimeError(f"PR #{pr.get('number')} is {pr.get('state')!r} (human action preserved).")
    if pr.get("isDraft") is not True:
        raise RuntimeError(f"Open PR #{pr.get('number')} ({context}) is not a Draft PR; refusing to overwrite.")
    if pr.get("baseRefName") != base_branch:
        raise RuntimeError(
            f"Open PR #{pr.get('number')} ({context}) targets '{pr.get('baseRefName')}', expected '{base_branch}'."
        )
    if candidate_sha is not None and pr.get("headRefOid") != candidate_sha:
        raise RuntimeError(
            f"Open PR #{pr.get('number')} ({context}) headRefOid ({str(pr.get('headRefOid') or 'missing')[:8]}) != reviewed SHA ({candidate_sha[:8]})."
        )
    return pr


def claim_metadata_matches(issue: dict[str, Any], state: dict[str, Any]) -> bool:
    expected = state.get("claimed_ownership") or {}
    return (
        ("assignee_id" not in expected or (issue.get("assignee") or {}).get("id", "") == expected["assignee_id"])
        and (not expected.get("description_sha") or sha256_text(issue.get("description") or "") == expected["description_sha"])
    )


def require_publish_ownership(issue: dict[str, Any], state: dict[str, Any]) -> None:
    live = issue["state"]
    claimed = state.get("claimed_state_id")
    in_claim = live.get("id") == claimed if claimed else live.get("name", "").lower() == "in progress"
    in_review = live.get("name", "").lower() == "in review"
    stage = state.get("stage")
    if (live.get("type") in ("completed", "canceled") or not claim_metadata_matches(issue, state)
            or not (in_review if stage == "linear_state_synced" else in_claim or (stage == "pr_ready" and in_review))):
        raise RuntimeError(f"Human Linear ownership conflict on {issue['identifier']} at {stage}: state {live.get('name')!r}.")


def _publish_destination(
    repo_root: Path, state: dict[str, Any], *, allow_branch_only: bool,
) -> tuple[bool, str | None]:
    remote_kind, gh_repo, raw_remote_url = classify_origin_remote(repo_root)
    if remote_kind == "none":
        raise RuntimeError("Publishing requires an origin remote.")
    expected_destination = state.get("publish_destination")
    if expected_destination:
        if sha256_text(gh_repo.lower() if gh_repo else raw_remote_url) != expected_destination:
            raise RuntimeError("Origin destination changed since preflight; refusing to publish.")
    elif not gh_repo or state.get("repo_identity") != canonical_repo_identity(repo_root):
        # Older checkpoints bound GitHub runs to owner/repo. For other remotes they bound only the local clone.
        raise RuntimeError("Legacy checkpoint cannot verify the publish destination. Review the configured origin and rerun.")
    force_gh = os.environ.get("AGENT_DISPATCH_FORCE_GH") == "1"
    if remote_kind == "github" or force_gh:
        is_github = True
    elif remote_kind in ("local_path", "none") or allow_branch_only:
        is_github = False
    else:
        raise RuntimeError(
            "Unrecognized non-GitHub origin remote; refusing silent branch-only delivery "
            f"(configure a standard GitHub origin URL or pass --allow-branch-only explicitly)."
        )

    return is_github, gh_repo


def _check_existing_pr(
    repo_root: Path, gh_repo: str | None, state: dict[str, Any], cur_stage: str,
) -> dict[str, Any] | None:
    # Recheck PR identity even on a late resume. A different open PR cannot replace the recorded one.
    branch = state["branch"]
    recorded_pr_num = state.get("pr_number")
    existing = fetch_open_pr_for_branch(repo_root, gh_repo, branch)
    if recorded_pr_num and (not existing or existing["number"] != recorded_pr_num):
        raise RuntimeError(f"Recorded PR #{recorded_pr_num} is no longer the open PR (human action preserved).")
    if existing:
        validate_open_draft_pr(existing, base_branch=state["base_branch"], context="pre-push check")
        state["pr_number"] = existing["number"]
    elif cur_stage in ("pr_ready", "linear_state_synced"):
        raise RuntimeError("Previously delivered PR is no longer open (human action preserved).")
    elif cur_stage == "pushed" or state.get("pr_create_attempted"):
        history = fetch_open_prs_for_branch(repo_root, gh_repo, branch, state="all")
        if any(pr.get("headRefOid") == state["candidate_sha"] for pr in history):
            raise RuntimeError("Previously delivered PR is no longer open (human action preserved).")
    return existing


def _push_reviewed_branch(
    repo_root: Path, run_dir: Path, latest_file: Path, state: dict[str, Any], cur_stage: str,
) -> None:
    branch = state["branch"]
    base_branch = state["base_branch"]
    candidate_sha = state["candidate_sha"]
    remote_sha = get_remote_branch_sha(repo_root, branch)
    already_pushed = cur_stage in ("pushed", "pr_ready", "linear_state_synced") or state.get("pushed")
    # Older checkpoints recorded only the merge base used for review.
    expected_base = state.get("target_sha") or state.get("base_sha")
    if not expected_base:
        raise RuntimeError("Publish checkpoint is missing the reviewed base commit.")
    check_remote_base(repo_root, base_branch, expected_base, "publication")
    if remote_sha != candidate_sha and already_pushed:
        raise RuntimeError(f"Remote origin/{branch} moved or was deleted after push; human action preserved.")
    if remote_sha != candidate_sha:
        if "pre_push_sha" in state and remote_sha != state["pre_push_sha"]:
            raise RuntimeError(f"Remote origin/{branch} changed after review; refusing to push.")
        if remote_sha:
            run_cmd(git_cmd("fetch", "origin", f"refs/heads/{branch}"), cwd=repo_root, check=True, env_role="orchestrator")
            anc = run_cmd(git_cmd("merge-base", "--is-ancestor", remote_sha, candidate_sha), cwd=repo_root, env_role="orchestrator")
            if anc.returncode != 0:
                raise RuntimeError(f"Remote origin/{branch} diverged from reviewed SHA; refusing to push.")
        state.update(push_attempted=True, stage="pushing", pre_push_sha=remote_sha)
        save_checkpoint(run_dir, latest_file, state)
        check_shutdown()
        # The lease closes the race with a human resetting/deleting the branch after the ancestry check.
        ref = f"refs/heads/{branch}"
        run_cmd(git_cmd("push", "--no-verify", f"--force-with-lease={ref}:{remote_sha}", "origin", f"{candidate_sha}:{ref}"),
                cwd=repo_root, check=True, env_role="orchestrator")
        if get_remote_branch_sha(repo_root, branch) != candidate_sha:
            raise RuntimeError("Remote SHA mismatch after push.")


def _create_draft_pr_with_retry(
    repo_root: Path,
    run_dir: Path,
    latest_file: Path,
    state: dict[str, Any],
    gh_repo: str | None,
    pr_title: str,
) -> None:
    branch = state["branch"]
    base_branch = state["base_branch"]
    state["pr_create_attempted"] = True
    save_checkpoint(run_dir, latest_file, state)
    body_file = run_dir / "pr-body.md"
    atomic_write_text(body_file, redact_secrets(state["pr_body"]))
    cmd = ["gh", "pr", "create", "--draft", "--base", base_branch, "--head", branch,
           "--title", pr_title, "--body-file", str(body_file)]
    if gh_repo:
        cmd += ["--repo", gh_repo]
    attempts = max(1, int(os.environ.get("AGENT_DISPATCH_PR_CREATE_ATTEMPTS", "3")))
    pause = float(os.environ.get("AGENT_DISPATCH_PR_CREATE_RETRY_SLEEP", "2.0"))
    for attempt_i in range(attempts):
        check_shutdown()
        try:
            created = run_cmd(cmd, cwd=repo_root, check=True, env_role="orchestrator")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            if attempt_i + 1 >= attempts or is_shutdown_requested():
                raise
            log(f"gh pr create failed transiently (attempt {attempt_i + 1}/{attempts}: {redact_secrets(str(exc))[:200]}); retrying...")
            if pause > 0:
                interruptible_sleep(pause)
            check_shutdown()
            # A transient 5xx/timeout may have created the PR server-side; verify idempotently before retrying.
            existing = fetch_open_pr_for_branch(repo_root, gh_repo, branch)
            if existing:
                validate_open_draft_pr(existing, base_branch=base_branch, context="PR create retry check")
                state["pr_number"] = existing["number"]
                save_checkpoint(run_dir, latest_file, state)
                return
            continue
        number = re.search(r"/pull/(\d+)/?$", created.stdout.strip())
        if number:
            state["pr_number"] = int(number.group(1))
            save_checkpoint(run_dir, latest_file, state)
        return


def _reconcile_draft_pr(
    repo_root: Path,
    run_dir: Path,
    latest_file: Path,
    state: dict[str, Any],
    gh_repo: str | None,
    pr_title: str,
    existing: dict[str, Any] | None,
) -> None:
    branch = state["branch"]
    base_branch = state["base_branch"]
    candidate_sha = state["candidate_sha"]
    if not existing:
        _create_draft_pr_with_retry(repo_root, run_dir, latest_file, state, gh_repo, pr_title)

    attempts = max(1, int(os.environ.get("AGENT_DISPATCH_PR_POLL_ATTEMPTS", "5")))
    pause = float(os.environ.get("AGENT_DISPATCH_PR_POLL_SLEEP", "2.0"))
    for attempt_i in range(attempts):
        check_shutdown()
        pr = fetch_open_pr_for_branch(repo_root, gh_repo, branch)
        if pr and pr.get("headRefOid") == candidate_sha:
            break
        if attempt_i + 1 < attempts:
            interruptible_sleep(pause)
    verified = validate_open_draft_pr(pr, base_branch=base_branch, candidate_sha=candidate_sha, context="PR read-back")
    if state.get("pr_number") and verified["number"] != state["pr_number"]:
        raise RuntimeError("Open PR changed during publication; human action preserved.")
    state.update(pr_number=verified["number"], pr_url=verified["url"], delivery_mode="draft_pr")


def _sync_linear_state(
    client: LinearClient, issue: dict[str, Any], run_dir: Path, latest_file: Path, state: dict[str, Any],
) -> dict[str, Any]:
    live_issue = client.get_issue(issue["id"])
    require_publish_ownership(live_issue, state)
    check_shutdown()
    if state["stage"] != "linear_state_synced":
        client.update_issue_state(live_issue, "In Review")
        state.update(linear_state_synced=True, stage="linear_state_synced")
        save_checkpoint(run_dir, latest_file, state)
    client.remove_labels_if_present(live_issue, {"dispatch:failed", "dispatch:publish-incomplete"})
    issue["state"] = live_issue["state"]
    issue["labels"] = live_issue["labels"]
    return live_issue


def _post_delivery_comment(client: LinearClient, live_issue: dict[str, Any], state: dict[str, Any]) -> None:
    candidate_sha = state["candidate_sha"]
    branch = state["branch"]
    # The stable marker makes the delivery summary safe to retry.
    marker = f"<!-- agent-dispatch-delivery:{state['run_id']}:{candidate_sha} -->"
    existing_comments = (live_issue.get("comments") or {}).get("nodes", [])
    already_posted = any(marker in (c.get("body") or "") for c in existing_comments)
    if not already_posted:
        rev = state["review"]
        pr_line = state["pr_url"] if state.get("pr_url") else "N/A (branch-only non-GitHub remote)"
        comment_lines = [
            marker,
            f"### [agent-dispatch] Ready for Human Merge (`{candidate_sha[:8]}`)",
            f"- **Draft PR**: {pr_line}",
            f"- **Branch**: `{branch}` (`{candidate_sha}`)",
            f"- **Worker / Reviewer**: `{state['worker']}` / `{state['reviewer']}`",
            f"- **Attempts**: {state['attempt']}/{state['max_attempts']}",
            f"- **Summary**: {redact_secrets(state['worker_summary'])}",
            f"- **Standards**: `{rev['standards']['verdict'].upper()}` — {redact_secrets(rev['standards']['summary'])}",
            f"- **Spec**: `{rev['spec']['verdict'].upper()}` — {redact_secrets(rev['spec']['summary'])}",
        ]
        auth_meta = state.get("gate_authorizations") or {}
        if any(auth_meta.values()):
            comment_lines.append(f"- **Gate/Test Authorizations**: `{json.dumps(auth_meta, ensure_ascii=False)}`")
        if state.get("discovered_issues"):
            comment_lines += ["", "**Discovered Side Issues (untouched):**"]
            comment_lines.extend(f"- {redact_secrets(d)}" for d in state["discovered_issues"])
        client.add_comment(live_issue["id"], "\n".join(comment_lines))


def reconcile_publish(
    client: LinearClient,
    issue: dict[str, Any],
    repo_root: Path,
    run_dir: Path,
    latest_file: Path,
    state: dict[str, Any],
    *,
    allow_branch_only: bool = False,
) -> dict[str, Any]:
    """
    Stage-aware, non-destructive remote reconciliation:
      - Respects already-completed stages (`pr_ready`, `linear_state_synced`) so resuming after a late comment failure
        NEVER recreates a human-closed PR, NEVER overwrites a human-edited PR body, and NEVER reverts human state edits.
      - Fails closed on unrecognized non-local remotes (unless `allow_branch_only=True`) and on >1 open PR sharing head branch.
      - Checks live Linear ownership/terminal state and PR compatibility BEFORE any remote push.
    """
    is_github, gh_repo = _publish_destination(repo_root, state, allow_branch_only=allow_branch_only)
    cur_stage = str(state.get("stage") or "verified_and_reviewed")

    live_pre = client.get_issue(issue["identifier"])
    require_publish_ownership(live_pre, state)

    pr_title = f"{issue['identifier']}: {issue['title']}"
    title_secrets = scan_text_for_secrets(pr_title, "PR title", allow_marker=False)
    if title_secrets:
        raise RuntimeError("Refusing to publish PR with secret in title: " + "; ".join(title_secrets))

    existing = _check_existing_pr(repo_root, gh_repo, state, cur_stage) if is_github else None
    _push_reviewed_branch(repo_root, run_dir, latest_file, state, cur_stage)

    if cur_stage not in ("pr_ready", "linear_state_synced"):
        state.update(pushed=True, push_attempted=True, stage="pushed")
        save_checkpoint(run_dir, latest_file, state)
        if is_github:
            _reconcile_draft_pr(repo_root, run_dir, latest_file, state, gh_repo, pr_title, existing)
        else:
            state.update(pr_url="", delivery_mode="branch_only")
        state["stage"] = "pr_ready"
        save_checkpoint(run_dir, latest_file, state)
    elif is_github:
        validate_open_draft_pr(existing, base_branch=state["base_branch"], candidate_sha=state["candidate_sha"], context=cur_stage)

    live_issue = _sync_linear_state(client, issue, run_dir, latest_file, state)
    _post_delivery_comment(client, live_issue, state)
    state["linear_comment_posted"] = True
    state["stage"] = "completed" if state.get("delivery_mode") == "draft_pr" else "completed_branch_only"
    save_checkpoint(run_dir, latest_file, state)
    return state


def verify_completed_publish_readonly(
    client: LinearClient,
    live_issue: dict[str, Any],
    repo_root: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Read-only verification for already-completed checkpoints: checks SHA, PR state/draft/base/head, AND Linear state."""
    branch = state["branch"]
    base_branch = state["base_branch"]
    candidate_sha = state["candidate_sha"]
    _, gh_repo, raw_remote_url = classify_origin_remote(repo_root)
    if state.get("publish_destination") and sha256_text(gh_repo.lower() if gh_repo else raw_remote_url) != state["publish_destination"]:
        raise RuntimeError("Origin destination changed since publication.")
    is_github = bool(gh_repo or os.environ.get("AGENT_DISPATCH_FORCE_GH") == "1")
    conflicts: list[str] = []

    remote_sha = get_remote_branch_sha(repo_root, branch)
    if remote_sha != candidate_sha:
        conflicts.append(f"Remote origin/{branch} SHA is '{remote_sha[:8] or 'deleted'}', expected '{candidate_sha[:8]}'.")

    live_st_name = str((live_issue.get("state") or {}).get("name") or "")
    if live_st_name.lower() != "in review":
        conflicts.append(f"Linear issue state drifted to '{live_st_name}' (expected 'In Review'; human action preserved).")

    if is_github and state.get("delivery_mode", "draft_pr") == "draft_pr":
        pr_num = state.get("pr_number")
        pr_obj = fetch_open_pr_for_branch(repo_root, gh_repo, branch)
        if not pr_obj or (pr_num and pr_obj["number"] != pr_num):
            conflicts.append(f"Delivered PR ({state.get('pr_url') or branch}) is no longer the open PR (human action preserved).")
        else:
            pr_state = str(pr_obj.get("state") or "OPEN").upper()
            if pr_state != "OPEN":
                conflicts.append(f"Delivered PR #{pr_obj.get('number')} is '{pr_state}' (human action preserved; refusing to reopen/recreate).")
            elif pr_obj.get("baseRefName") != base_branch:
                conflicts.append(f"Delivered PR #{pr_obj.get('number')} base changed to '{pr_obj.get('baseRefName')}' (expected '{base_branch}').")
            elif pr_obj.get("headRefOid") != candidate_sha:
                conflicts.append(f"Delivered PR #{pr_obj.get('number')} headRefOid is '{str(pr_obj.get('headRefOid') or '')[:8]}', expected '{candidate_sha[:8]}'.")
            elif pr_obj.get("isDraft") is not True:
                conflicts.append(f"Delivered PR #{pr_obj.get('number')} was marked Ready for Review by a human (preserving without overwrite).")

    if conflicts:
        raise RuntimeError(
            "Checkpoint is already completed, and remote state diverged (likely human action); refusing to overwrite:\n"
            + "\n".join(f"- {c}" for c in conflicts)
        )
    return {
        "issue": live_issue["identifier"],
        "status": "passed_branch_only" if state["stage"] == "completed_branch_only" else "passed",
        "reconciled": False,
        "verified_readonly": True,
        "commit": candidate_sha,
        "pr_url": state.get("pr_url", ""),
        "linear_state": live_st_name,
        "run_dir": state.get("run_dir", ""),
        "sessions": state.get("sessions", []),
    }
