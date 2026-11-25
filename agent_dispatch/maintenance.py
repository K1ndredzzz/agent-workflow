"""Dispatch status, environment checks, and managed worktree cleanup."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from .brief import parse_touches
from .config import PRELOADED_REFS, SKILLS_ROOT, _cache_root, _worktree_cache_path
from .gates import detect_verify_command
from .git_ops import (
    check_dirty_docs_in_main_repo,
    detect_default_branch,
    fetch_base_ref,
    get_git_common_dir,
    get_repo_root,
    parse_github_owner_repo,
    read_worktree_lock_owner,
    repo_cache_key,
    ticket_lock_path,
)
from .linear import LinearClient, resolve_linear_auth, resolve_project
from .process import file_lock, find_harness_binary, git_cmd, is_locked, run_cmd
from .scheduler import collect_runnable_tickets
from .secrets import redact_secrets, safe_json


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = get_repo_root(args.repo)
    rkey = repo_cache_key(repo_root)
    client = LinearClient(resolve_linear_auth())
    proj = resolve_project(client, repo_root, args.project)
    unstarted = client.list_project_issues(proj["id"], state_type="unstarted")
    started = client.list_project_issues(proj["id"], state_type="started")
    runnable, blocked, skipped = collect_runnable_tickets(client, unstarted)

    wt_root = _worktree_cache_path(rkey)
    active_wts: list[dict[str, Any]] = []
    if wt_root.is_dir():
        for p in sorted(wt_root.iterdir()):
            if not p.is_dir() or p.name.startswith(".baseline-"):
                continue
            issue_uuid, _, ckpt, owner_err = read_worktree_lock_owner(repo_root, rkey, p)
            if issue_uuid:
                locked_bool = is_locked(ticket_lock_path(repo_root, issue_uuid))
                lock_state = "locked" if locked_bool else "unlocked"
            else:
                locked_bool = None
                lock_state = "unknown"
            entry: dict[str, Any] = {
                "id": p.name,
                "path": str(p),
                "locked": locked_bool,
                "lock_state": lock_state,
                "last_stage": ckpt.get("stage"),
                "pr_url": ckpt.get("pr_url"),
                "run_dir": ckpt.get("run_dir"),
            }
            if owner_err:
                entry["owner_warning"] = owner_err
            active_wts.append(entry)

    in_prog_list: list[dict[str, Any]] = []
    for i in started:
        if (i.get("children") or {}).get("nodes"):
            continue
        locked = is_locked(ticket_lock_path(repo_root, i["id"]))
        st_name = (i.get("state") or {}).get("name")
        in_prog_list.append(
            {
                "id": i["identifier"],
                "title": i["title"],
                "state": st_name,
                "active_lock": locked,
                "zombie_warning": (st_name == "In Progress" and not locked),
            }
        )

    print(
        safe_json(
            {
                "repo": str(repo_root),
                "linear_project": proj["name"],
                "frontier_ready": [
                    {"id": i["identifier"], "title": i["title"], "touches": sorted(parse_touches(i.get("description") or ""))}
                    for i in runnable
                ],
                "blocked": blocked,
                "skipped_not_ready": skipped,
                "in_progress_or_review": in_prog_list,
                "active_worktrees": active_wts,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: dict[str, Any] = {}
    ok_all = True

    missing_rubrics = [k for k in ("tdd", "standards", "spec") if not PRELOADED_REFS.get(k, "").strip()]
    checks["rubrics"] = {"ok": not missing_rubrics, "skills_root": str(SKILLS_ROOT), "missing": missing_rubrics}
    if missing_rubrics:
        ok_all = False

    try:
        client = LinearClient(resolve_linear_auth())
        teams, projects = client.list_teams_and_projects()
        team_states = {t["key"]: [s["name"] for s in (t.get("states") or {}).get("nodes", [])] for t in teams}
        checks["linear_auth"] = {"ok": True, "teams": team_states, "projects": [p["name"] for p in projects]}
    except Exception as exc:
        ok_all = False
        checks["linear_auth"] = {"ok": False, "error": redact_secrets(str(exc))}
        client = None

    try:
        gh_res = run_cmd(["gh", "auth", "status"], env_role="orchestrator")
        checks["gh_auth"] = {"ok": gh_res.returncode == 0}
        if gh_res.returncode != 0:
            ok_all = False
    except FileNotFoundError:
        ok_all = False
        checks["gh_auth"] = {"ok": False, "error": "`gh` CLI binary not found in PATH."}

    harnesses: dict[str, Any] = {}
    for name in ("codex", "agy", "opencode", "pi"):
        p = find_harness_binary(name)
        harnesses[name] = {"ok": bool(p), "path": p}
        if name in ("codex", "agy") and not p:
            ok_all = False
    checks["harnesses"] = harnesses

    try:
        repo_root = get_repo_root(args.repo)
        base_branch = detect_default_branch(repo_root)
        gh_repo = parse_github_owner_repo(repo_root)
        verify_cmd = detect_verify_command(repo_root, None)
        dirty_docs = check_dirty_docs_in_main_repo(repo_root, base_branch)
        proj_info: dict[str, Any] = {}
        if client:
            try:
                p = resolve_project(client, repo_root, args.project)
                proj_info = {"ok": True, "project": p["name"]}
            except Exception as exc:
                ok_all = False
                proj_info = {"ok": False, "error": str(exc)}
        checks["repo"] = {
            "ok": bool(verify_cmd) and not dirty_docs and proj_info.get("ok", True),
            "root": str(repo_root),
            "default_branch": base_branch,
            "github_remote": gh_repo,
            "verify_cmd": verify_cmd,
            "verify_detected": bool(verify_cmd),
            "dirty_docs": dirty_docs,
            "linear_project": proj_info,
        }
        if not verify_cmd or dirty_docs:
            ok_all = False
    except Exception as exc:
        ok_all = False
        checks["repo"] = {"ok": False, "error": str(exc)}

    print(safe_json({"ok": ok_all, "checks": checks}, indent=2, ensure_ascii=False))
    return 0 if ok_all else 1


def cleanup_baseline(repo_root: Path, rkey: str, wt: Path, force: bool) -> None:
    owner_file = _cache_root() / "baseline-owners" / rkey / wt.name
    if not owner_file.is_file() or owner_file.is_symlink() or wt.is_symlink():
        raise RuntimeError("missing baseline ownership")
    owner = json.loads(owner_file.read_text(encoding="utf-8"))
    if not isinstance(owner, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(owner.get("key", ""))):
        raise RuntimeError("invalid baseline ownership")
    lock = _cache_root() / "locks" / f"baseline-{owner['key'][:16]}.lock"
    with file_lock(lock, blocking=False):
        head = run_cmd(git_cmd("rev-parse", "HEAD"), cwd=wt, check=True, env_role="orchestrator").stdout.strip()
        branch = run_cmd(git_cmd("branch", "--show-current"), cwd=wt, check=True, env_role="orchestrator").stdout.strip()
        if branch or head != owner.get("base_sha") or wt.resolve() != get_repo_root(str(wt)):
            raise RuntimeError("baseline ownership changed")
        tracked_dirty = run_cmd(
            git_cmd("status", "--porcelain", "--untracked-files=no"), cwd=wt, check=True, env_role="orchestrator"
        ).stdout.strip()
        if tracked_dirty and not force:
            raise RuntimeError("baseline has modified tracked files")
        # Allow reclaiming detached baselines with untracked bootstrap/verify artifacts (e.g., node_modules/.venv)
        _worktree_cache_path(rkey, wt.name)
        run_cmd(git_cmd("worktree", "remove", "--force", str(wt)), cwd=repo_root, check=True, env_role="orchestrator")
        owner_file.unlink()


def cmd_cleanup(args: argparse.Namespace) -> int:
    repo_root = get_repo_root(args.repo)
    repo_common = get_git_common_dir(repo_root)
    rkey = repo_cache_key(repo_root)
    base_branch = detect_default_branch(repo_root)
    gh_repo = parse_github_owner_repo(repo_root)
    use_gh = bool(gh_repo or os.environ.get("AGENT_DISPATCH_FORCE_GH") == "1")
    wt_root = _worktree_cache_path(rkey)
    removed: list[str] = []
    skipped: list[str] = []

    with file_lock(_cache_root() / "locks" / f"repo-{rkey}.lock", blocking=True):
        base_ref = fetch_base_ref(repo_root, base_branch)
        base_sha = run_cmd(git_cmd("rev-parse", base_ref), cwd=repo_root, check=True, env_role="orchestrator").stdout.strip()

        if wt_root.is_dir():
            for wt in sorted(wt_root.iterdir()):
                if wt.is_symlink():
                    skipped.append(f"{wt.name} (symlink in worktree root; refusing delete)")
                    continue
                if not wt.is_dir():
                    continue
                if not (wt / ".git").exists():
                    skipped.append(f"{wt.name} (not a git worktree)")
                    continue
                if get_git_common_dir(wt) != repo_common:
                    skipped.append(f"{wt.name} (git-common-dir ownership mismatch; refusing delete)")
                    continue
                if wt.name.startswith(".baseline-"):
                    try:
                        cleanup_baseline(repo_root, rkey, wt, args.all)
                        removed.append(wt.name)
                    except (OSError, RuntimeError, ValueError) as exc:
                        skipped.append(f"{wt.name} ({redact_secrets(str(exc))})")
                    continue
                issue_uuid, owner_branch, _, owner_err = read_worktree_lock_owner(repo_root, rkey, wt)
                if not issue_uuid or not owner_branch:
                    skipped.append(f"{wt.name} ({owner_err or 'missing_lock_owner_metadata'}; refusing unsafe delete)")
                    continue

                try:
                    with file_lock(ticket_lock_path(repo_root, issue_uuid), blocking=False):
                        removed_ok, report = _cleanup_ticket_worktree(
                            repo_root, rkey, wt, owner_branch,
                            force=args.all, base_branch=base_branch, base_sha=base_sha, gh_repo=gh_repo, use_gh=use_gh,
                        )
                        (removed if removed_ok else skipped).append(report)
                except RuntimeError as exc:
                    skipped.append(f"{wt.name} ({redact_secrets(str(exc))})")
        run_cmd(git_cmd("worktree", "prune"), cwd=repo_root, env_role="orchestrator")

    print(safe_json({"removed": removed, "skipped": skipped}, indent=2, ensure_ascii=False))
    return 0


def _cleanup_ticket_worktree(
    repo_root: Path,
    rkey: str,
    worktree: Path,
    owner_branch: str,
    *,
    force: bool,
    base_branch: str,
    base_sha: str,
    gh_repo: str | None,
    use_gh: bool,
) -> tuple[bool, str]:
    name = worktree.name
    branch = run_cmd(git_cmd("rev-parse", "--abbrev-ref", "HEAD"), cwd=worktree, env_role="orchestrator").stdout.strip()
    if branch != owner_branch:
        # Preserve personal branches and detached HEAD even with --all.
        return False, f"{name} (branch ownership mismatch: checked out '{branch}', owner '{owner_branch}'; refusing unsafe delete)"
    dirty = run_cmd(git_cmd("status", "--porcelain", "-z"), cwd=worktree, check=True, env_role="orchestrator").stdout
    if dirty and not force:
        return False, f"{name} (uncommitted changes)"
    head_sha = run_cmd(git_cmd("rev-parse", "HEAD"), cwd=worktree, env_role="orchestrator").stdout.strip()
    if not force and not _is_merged(repo_root, branch, head_sha, base_branch, base_sha, gh_repo, use_gh):
        return False, f"{name} ({branch}@{head_sha[:8]}, unmerged or newer local commits)"

    current_ref = run_cmd(git_cmd("symbolic-ref", "-q", "HEAD"), cwd=worktree, env_role="orchestrator")
    current_sha = run_cmd(git_cmd("rev-parse", "HEAD"), cwd=worktree, check=True, env_role="orchestrator")
    ref = f"refs/heads/{branch}"
    if current_ref.stdout.strip() != ref or current_sha.stdout.strip() != head_sha:
        return False, f"{name} (branch or HEAD changed during cleanup)"
    _worktree_cache_path(rkey, name)
    rm_cmd = git_cmd("worktree", "remove", *(["--force"] if force else []), str(worktree))
    rm_res = run_cmd(rm_cmd, cwd=repo_root, env_role="orchestrator")
    if rm_res.returncode != 0:
        return False, f"{name} (remove failed: {rm_res.stderr.strip()[:120]})"

    if branch and branch not in (base_branch, "HEAD"):
        checked_out = run_cmd(
            git_cmd("for-each-ref", "--format=%(worktreepath)", ref), cwd=repo_root, check=True, env_role="orchestrator"
        )
        if checked_out.stdout.strip():
            return False, f"{name} (worktree removed, branch now checked out elsewhere)"
        # Compare-and-delete after removing the worktree. Preserve any newer commit.
        del_res = run_cmd(git_cmd("update-ref", "--no-deref", "-d", ref, head_sha), cwd=repo_root, env_role="orchestrator")
        if del_res.returncode != 0:
            return False, f"{name} (worktree removed, branch delete failed: {del_res.stderr.strip()[:100]})"

    return True, f"{name} ({branch}@{head_sha[:8]})"


def _is_merged(
    repo_root: Path,
    branch: str,
    head_sha: str,
    base_branch: str,
    base_sha: str,
    gh_repo: str | None,
    use_gh: bool,
) -> bool:
    if use_gh and branch:
        pr_cmd = ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number,headRefOid,baseRefName"]
        if gh_repo:
            pr_cmd += ["--repo", gh_repo]
        pr_result = run_cmd(pr_cmd, cwd=repo_root, env_role="orchestrator")
        if pr_result.returncode == 0:
            for pr in json.loads(pr_result.stdout or "[]"):
                if pr.get("headRefOid") == head_sha and pr.get("baseRefName") == base_branch:
                    return True

    if not head_sha or head_sha == base_sha:
        return False
    ancestry = run_cmd(git_cmd("merge-base", "--is-ancestor", head_sha, base_sha), cwd=repo_root, env_role="orchestrator")
    return ancestry.returncode == 0
