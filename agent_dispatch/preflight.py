"""Prepare ticket worktrees and initialize runs before a Claim."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .brief import (
    classify_blockers, is_ready_for_frontier, parse_authorized_changes, parse_touches,
    path_matches_any_pattern, render_issue_snapshot, snapshot_baseline_standards,
)
from .config import _cache_root
from .gates import (
    check_base_baseline, detect_verify_command, file_identity,
    release_setup_files, scan_before_git_write, untracked_files,
)
from .git_ops import (
    bootstrap_worktree,
    canonical_repo_identity,
    check_dirty_docs_in_main_repo,
    check_remote_fast_forward_ready,
    classify_origin_remote,
    detect_default_branch,
    ensure_dispatch_excluded,
    get_git_common_dir,
    parse_github_owner_repo,
    prepare_worktree,
    repo_cache_key,
)
from .harness import HarnessSpec
from .linear import LinearClient
from .process import atomic_write_text, git_cmd, log, run_cmd, sha256_file, sha256_text
from .publish import PUBLISH_STAGES, fetch_open_pr_for_branch, save_checkpoint, validate_open_draft_pr
from .secrets import redact_secrets, scan_text_for_secrets


@dataclass(frozen=True)
class RunOptions:
    verify_cmd_override: str | None
    setup_cmd_override: str | None
    max_rework: int
    verify_timeout: int
    setup_timeout: int
    worker_timeout: int
    review_timeout: int
    no_push: bool
    allow_no_verify: bool
    allow_gate_edits: bool
    allow_test_edits: bool
    allow_dirty_docs: bool
    allow_branch_only: bool
    skip_baseline: bool
    skip_project_check: bool
    force_rerun: bool
    fresh: bool
    auto_claimed: bool
    reserved_touches: set[str] | None


@dataclass(frozen=True)
class RepositoryPreflight:
    root: Path
    key: str
    common_dir: Path
    base_branch: str
    publish_destination: str


@dataclass(frozen=True)
class PreparedWorktree:
    path: Path
    branch: str
    base_sha: str
    target_sha: str
    gh_repo: str | None
    pre_branch_sha: str
    preflight_pr: dict[str, Any] | None
    setup_files: dict[str, tuple[int, int, int, int]]
    verify_cmd: str | None


@dataclass
class TicketRun:
    client: LinearClient
    repo: RepositoryPreflight
    issue: dict[str, Any]
    worker: HarnessSpec
    reviewer: HarnessSpec
    options: RunOptions
    checkout: PreparedWorktree
    run_id: str
    run_dir: Path
    latest_file: Path
    auth_brief_path: Path
    auth_brief_sha256: str
    baseline_std_dir: Path
    baseline_std_hashes: dict[str, str]
    standard_paths: list[Path]
    worker_artifacts: dict[Path, str]
    allow_root_gitattributes: bool
    state: dict[str, Any]
    claimed_state_id: str
    sessions: list[dict[str, Any]] = field(default_factory=list)
    pre_git_cfg: dict[str, str] = field(default_factory=dict)
    expected_ownership: dict[str, str] = field(default_factory=dict)
    claim_attempted: bool = False
    moved_in_progress: bool = False
    publish_started: bool = False
    terminal_ok: bool = False
    failure_reason: str = ""

    @property
    def identifier(self) -> str:
        return self.issue["identifier"]

    def save(self) -> None:
        save_checkpoint(self.run_dir, self.latest_file, self.state)

    def stash(self, reason: str) -> None:
        release_setup_files(self.checkout.path, self.checkout.setup_files)
        stash_dirty_worktree_for_safe_rerun(
            self.checkout.path, f"{self.run_id}-{reason}", attr_source=self.checkout.base_sha,
        )


def stash_dirty_worktree_for_safe_rerun(worktree: Path, label: str, *, attr_source: str | None = None) -> None:
    """Stash safe edits, leaving potentially sensitive working files and index entries untouched."""
    try:
        ensure_dispatch_excluded(worktree, ignore_shutdown=True)
        dirty = run_cmd(git_cmd("status", "--porcelain"), cwd=worktree, check=True, env_role="orchestrator", ignore_shutdown=True).stdout.strip()
        if dirty:
            if scan_before_git_write(worktree, for_stash=True, attr_source=attr_source):
                log("Secret scan blocked automatic stash. Working files and index were preserved for manual inspection.")
                return
            run_cmd(
                git_cmd("stash", "push", "-u", "-m", f"agent-dispatch-{label}"),
                cwd=worktree,
                check=True,
                env_role="orchestrator",
                ignore_shutdown=True,
            )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        log(f"Could not safely stash working files: {redact_secrets(str(exc))}")


def inspect_repository(repo_root: Path, options: RunOptions) -> RepositoryPreflight:
    rkey = repo_cache_key(repo_root)
    repo_common = get_git_common_dir(repo_root)
    base_branch = detect_default_branch(repo_root)

    publish_destination = ""
    if not options.no_push:
        remote_kind, remote_repo, raw_remote_url = classify_origin_remote(repo_root)
        if remote_kind == "none":
            raise RuntimeError("Publishing requires an origin remote. Configure origin or use --no-push.")
        publish_destination = sha256_text(remote_repo.lower() if remote_repo else raw_remote_url)
        if remote_kind == "unknown_remote" and not options.allow_branch_only and os.environ.get("AGENT_DISPATCH_FORCE_GH") != "1":
            raise RuntimeError(
                "Unrecognized non-GitHub origin remote; refusing silent branch-only delivery "
                f"(configure a standard GitHub URL or pass --allow-branch-only)."
            )

    if not options.allow_dirty_docs:
        dirty_doc_msgs = check_dirty_docs_in_main_repo(repo_root, base_branch)
        if dirty_doc_msgs:
            raise RuntimeError(
                "Main repo has uncommitted/unpushed domain docs; land them on base branch first (or pass --allow-dirty-docs):\n"
                + "\n".join(dirty_doc_msgs)
            )
    return RepositoryPreflight(repo_root, rkey, repo_common, base_branch, publish_destination)


def _check_previous_checkpoint(latest_file: Path, identifier: str, options: RunOptions) -> None:
    if not options.no_push and not options.force_rerun and latest_file.is_file():
        try:
            latest = json.loads(latest_file.read_text(encoding="utf-8"))
            if not isinstance(latest, dict) or not latest.get("run_dir"):
                raise ValueError("missing run_dir")
            authoritative = Path(latest["run_dir"]) / "state.json"
            previous = json.loads(authoritative.read_text(encoding="utf-8")) if authoritative.is_file() else latest
            if not isinstance(previous, dict):
                raise ValueError("state is not a JSON object")
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Corrupt checkpoint at {latest_file} ({exc}); inspect manually or pass --force-rerun."
            ) from exc
        if previous.get("stage") in ("claim_pending", "rollback_pending", *PUBLISH_STAGES):
            raise RuntimeError(f"Checkpoint at {previous['stage']}; run agent-dispatch resume {identifier} first.")


def _check_issue(
    issue: dict[str, Any], repo_project: dict[str, Any] | None, options: RunOptions, run_dir: Path | None = None,
) -> dict[str, Any] | None:
    if not options.skip_project_check and repo_project:
        iss_proj = issue.get("project") or {}
        if iss_proj.get("id") != repo_project["id"]:
            raise RuntimeError(
                f"Cross-repo guard: {issue['identifier']} belongs to Linear project "
                f"'{iss_proj.get('name', 'None')}', but current repo maps to '{repo_project['name']}'."
            )
    label_names_l = {l["name"].lower() for l in (issue.get("labels") or {}).get("nodes", [])}
    if (issue.get("children") or {}).get("nodes") or "spec" in label_names_l:
        raise RuntimeError(
            f"{issue['identifier']} is a Spec/parent container (not a leaf slice); use `spec {issue['identifier']}`."
        )
    st_type = (issue.get("state") or {}).get("type", "")
    if st_type in ("completed", "canceled"):
        raise RuntimeError(f"{issue['identifier']} is already {issue['state']['name']}; refusing to run.")
    if options.auto_claimed:
        if options.reserved_touches is not None and options.reserved_touches != parse_touches(issue.get("description") or ""):
            return {"issue": issue["identifier"], "status": "deferred", "reason": "touches_changed_before_claim"}
        ready_ok, ready_reason = is_ready_for_frontier(issue)
        if not ready_ok:
            return {"issue": issue["identifier"], "status": "skipped_changed_before_claim", "reason": ready_reason}
    elif st_type == "started" and not options.force_rerun:
        raise RuntimeError(
            f"{issue['identifier']} is already '{issue['state']['name']}'. Pass --force-rerun (or `resume`) to proceed."
        )
    active_blks, canceled_blks = classify_blockers(issue)
    if active_blks or canceled_blks:
        return {
            "issue": issue["identifier"],
            "status": "blocked_by_dependencies",
            "active_blockers": active_blks,
            "canceled_blockers": canceled_blks,
        }
    if not options.no_push:
        team_state_names = {s["name"].lower() for s in (issue.get("team") or {}).get("states", {}).get("nodes", [])}
        for req_state in ("in progress", "todo", "in review"):
            if req_state not in team_state_names:
                raise RuntimeError(f"Required Linear workflow state '{req_state}' missing in team {issue['team']['key']}.")
    title_hits = scan_text_for_secrets(str(issue.get("title") or ""), "ticket title", allow_marker=False)
    if title_hits:
        return {
            "issue": issue["identifier"],
            "status": "secret_in_metadata",
            "error": "Secret detected in ticket title before dispatch:\n" + "\n".join(f"- {hit}" for hit in title_hits),
            **({"run_dir": str(run_dir)} if run_dir is not None else {}),
            "sessions": [],
        }
    return None


def prepare_ticket(
    client: LinearClient, repo: RepositoryPreflight, repo_project: dict[str, Any] | None,
    issue: dict[str, Any], worker: HarnessSpec, reviewer: HarnessSpec, options: RunOptions, run_cleanup: ExitStack,
) -> TicketRun | dict[str, Any]:
    """Run bootstrap and baseline checks under the ticket lock before attempting a Claim."""
    ticket_runs = _cache_root() / "runs" / repo.key / issue["identifier"]
    latest_file = ticket_runs / ("latest-local.json" if options.no_push else "latest.json")
    _check_previous_checkpoint(latest_file, issue["identifier"], options)
    issue = client.get_issue(issue["id"])
    early_result = _check_issue(issue, repo_project, options)
    if early_result is not None:
        return early_result

    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run_dir = _cache_root() / "runs" / repo.key / issue["identifier"] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    worktree, branch, target_sha = prepare_worktree(
        repo.root, issue["identifier"], repo.base_branch, fresh=options.fresh, issue_uuid=issue["id"]
    )
    base_sha = run_cmd(git_cmd("merge-base", target_sha, "HEAD"), cwd=worktree,
                       check=True, env_role="orchestrator").stdout.strip()
    preflight_pr = None
    pre_branch_sha = ""
    gh_repo = parse_github_owner_repo(repo.root)
    if not options.no_push:
        pre_branch_sha = check_remote_fast_forward_ready(repo.root, worktree, branch)
        if gh_repo or os.environ.get("AGENT_DISPATCH_FORCE_GH") == "1":
            preflight_pr = fetch_open_pr_for_branch(repo.root, gh_repo, branch)
            if preflight_pr:
                validate_open_draft_pr(preflight_pr, base_branch=repo.base_branch, context="preflight PR check")

    before_setup = untracked_files(worktree)
    bootstrap_worktree(worktree, options.setup_cmd_override, timeout=options.setup_timeout)
    setup_files = {name: file_identity(worktree / name) for name in untracked_files(worktree) - before_setup}
    if setup_files:
        run_cleanup.callback(release_setup_files, worktree, setup_files)
    verify_cmd = detect_verify_command(worktree, options.verify_cmd_override)
    if not verify_cmd and not options.allow_no_verify:
        return {
            "issue": issue["identifier"],
            "status": "no_verify_command",
            "error": "No deterministic verification command detected (pass --verify-cmd or --allow-no-verify).",
            "run_dir": str(run_dir),
        }

    if not options.skip_baseline:
        base_check = check_base_baseline(
            repo.root,
            repo.key,
            worktree,
            target_sha,
            options.verify_cmd_override,
            run_dir,
            setup_cmd_override=options.setup_cmd_override,
            setup_timeout=options.setup_timeout,
            allow_no_verify=options.allow_no_verify,
            timeout=options.verify_timeout,
        )
        if not base_check["passed"]:
            return {
                "issue": issue["identifier"],
                "status": "base_branch_red",
                "base_sha": target_sha,
                "verify": base_check,
                "run_dir": str(run_dir),
            }

    # Respect human edits made during bootstrap or baseline verification.
    issue = client.get_issue(issue["id"])
    early_result = _check_issue(issue, repo_project, options, run_dir)
    if early_result is not None:
        return early_result

    checkout = PreparedWorktree(
        worktree, branch, base_sha, target_sha, gh_repo, pre_branch_sha, preflight_pr, setup_files, verify_cmd,
    )
    return _start_run(client, repo, issue, worker, reviewer, options, checkout, run_id, run_dir, latest_file)


def _start_run(
    client: LinearClient, repo: RepositoryPreflight, issue: dict[str, Any], worker: HarnessSpec,
    reviewer: HarnessSpec, options: RunOptions, checkout: PreparedWorktree, run_id: str,
    run_dir: Path, latest_file: Path,
) -> TicketRun:
    pre_claim_state_id = str((issue.get("state") or {}).get("id") or "")
    pre_claim_state_name = str((issue.get("state") or {}).get("name") or "Todo")
    in_prog_state_id = next(
        (str(s["id"]) for s in (issue.get("team") or {}).get("states", {}).get("nodes", []) if s["name"].lower() == "in progress"),
        "",
    )

    # Authoritative immutable artifacts (SHA-256 verified before and after review)
    auth_brief_content = render_issue_snapshot(issue)
    auth_brief_path = run_dir / "brief.md"
    atomic_write_text(auth_brief_path, auth_brief_content)
    auth_brief_sha256 = sha256_file(auth_brief_path)
    wt_brief_path = checkout.path / ".dispatch" / f"{issue['identifier']}.md"
    wt_brief_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(wt_brief_path, auth_brief_content)

    baseline_std_dir = run_dir / "baseline-standards"
    _, baseline_std_hashes = snapshot_baseline_standards(checkout.path, checkout.base_sha, baseline_std_dir)
    worker_artifacts = {baseline_std_dir / rel: digest for rel, digest in baseline_std_hashes.items()}
    standard_paths = list(worker_artifacts)
    worker_artifacts[auth_brief_path] = auth_brief_sha256

    brief_gate_pats, brief_test_pats = parse_authorized_changes(issue.get("description") or "")
    allow_root_gitattributes = options.allow_gate_edits or path_matches_any_pattern(".gitattributes", brief_gate_pats)
    gate_authorizations = {
        "cli_allow_gate_edits": options.allow_gate_edits,
        "cli_allow_test_edits": options.allow_test_edits,
        "brief_gate_patterns": brief_gate_pats,
        "brief_test_patterns": brief_test_pats,
    }

    state: dict[str, Any] = {
        "issue": issue["identifier"],
        "issue_uuid": issue["id"],
        "run_id": run_id,
        "run_dir": str(run_dir),
        "repo_root": str(repo.root),
        "repo_identity": canonical_repo_identity(repo.root),
        "worktree": str(checkout.path),
        "branch": checkout.branch,
        "base_branch": repo.base_branch,
        "base_sha": checkout.base_sha,
        "target_sha": checkout.target_sha,
        "worker": worker.label,
        "reviewer": reviewer.label,
        "no_push": options.no_push,
        "allow_branch_only": options.allow_branch_only,
        "publish_destination": repo.publish_destination,
        "claimed_state_id": in_prog_state_id,
        "claimed_ownership": {"description_sha": sha256_text(issue.get("description") or ""),
                              "assignee_id": (issue.get("assignee") or {}).get("id", "")},
        "pre_claim_state_id": pre_claim_state_id,
        "pre_claim_state_name": pre_claim_state_name,
        "gate_authorizations": gate_authorizations,
        "stage": "preflight_ok",
        "push_attempted": False,
        "pushed": False,
    }
    run = TicketRun(
        client, repo, issue, worker, reviewer, options, checkout, run_id, run_dir, latest_file,
        auth_brief_path, auth_brief_sha256, baseline_std_dir, baseline_std_hashes, standard_paths,
        worker_artifacts, allow_root_gitattributes, state, in_prog_state_id,
    )
    run.save()
    return run

