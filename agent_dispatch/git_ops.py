"""Git repository identity, remote checks, and managed worktree lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from agent_dispatch.config import _cache_root, _worktree_cache_path
from agent_dispatch.process import atomic_write_text, file_lock, git_cmd, log, run_cmd, sha256_file, sha256_text
from agent_dispatch.secrets import redact_secrets


def repo_cache_key(repo_root: Path) -> str:
    digest = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{repo_root.name}-{digest}"


def canonical_repo_identity(repo_root: Path) -> str:
    """Canonical identity shared across clones of the same GitHub remote (or git-common-dir)."""
    gh_repo = parse_github_owner_repo(repo_root)
    if gh_repo:
        return hashlib.sha256(f"github:{gh_repo.lower()}".encode("utf-8")).hexdigest()[:12]
    common = get_git_common_dir(repo_root)
    return hashlib.sha256(f"local:{common}".encode("utf-8")).hexdigest()[:12]


def ticket_lock_path(repo_root: Path, issue_uuid: str) -> Path:
    return _cache_root() / "locks" / f"ticket-{canonical_repo_identity(repo_root)}-{issue_uuid}.lock"


def get_repo_root(repo_arg: str | None) -> Path:
    start = Path(repo_arg).resolve() if repo_arg else Path.cwd().resolve()
    res = run_cmd(git_cmd("rev-parse", "--show-toplevel"), cwd=start, env_role="orchestrator")
    if res.returncode != 0:
        raise RuntimeError(f"Not inside a git repository: {start}")
    return Path(res.stdout.strip()).resolve()


def get_git_common_dir(path: Path) -> Path:
    res = run_cmd(git_cmd("rev-parse", "--git-common-dir"), cwd=path, check=True, env_role="orchestrator")
    p = Path(res.stdout.strip())
    return p.resolve() if p.is_absolute() else (path / p).resolve()


def detect_default_branch(repo_root: Path) -> str:
    res = run_cmd(git_cmd("symbolic-ref", "refs/remotes/origin/HEAD"), cwd=repo_root, env_role="orchestrator")
    if res.returncode == 0:
        ref = res.stdout.strip()
        if ref.startswith("refs/remotes/origin/"):
            return ref.removeprefix("refs/remotes/origin/")
    for cand in ("main", "master"):
        if run_cmd(git_cmd("rev-parse", "--verify", f"refs/remotes/origin/{cand}"), cwd=repo_root, env_role="orchestrator").returncode == 0:
            return cand
        if run_cmd(git_cmd("rev-parse", "--verify", f"refs/heads/{cand}"), cwd=repo_root, env_role="orchestrator").returncode == 0:
            return cand
    raise RuntimeError(f"Cannot determine default branch (main/master) for {repo_root}")


def fetch_base_ref(repo_root: Path, branch: str) -> str:
    if run_cmd(git_cmd("remote", "get-url", "origin"), cwd=repo_root, env_role="orchestrator").returncode != 0:
        return f"refs/heads/{branch}"
    ref = f"refs/remotes/origin/{branch}"
    run_cmd(git_cmd("fetch", "origin", f"+refs/heads/{branch}:{ref}"),
            cwd=repo_root, check=True, env_role="orchestrator")
    return ref


def parse_github_owner_repo_url(url: str) -> str | None:
    """Parse GitHub owner/repo across HTTPS, SSH, ssh://git@ssh.github.com:443/..., and trailing .git/ variants."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return None
    m = re.match(
        r"^(?:https?://(?:[^/@]+@)?github\.com/|ssh://(?:[^/@]+@)?(?:ssh\.)?github\.com(?::\d+)?/|git@(?:ssh\.)?github\.com:)([^/\s]+/[^/\s]+?)(?:\.git)?$",
        u,
        re.I,
    )
    return m.group(1) if m else None


def parse_github_owner_repo(repo_root: Path) -> str | None:
    res = run_cmd(git_cmd("remote", "get-url", "origin"), cwd=repo_root, env_role="orchestrator")
    if res.returncode != 0:
        return None
    return parse_github_owner_repo_url(res.stdout.strip())


def classify_origin_remote(repo_root: Path) -> tuple[str, str | None, str]:
    """
    Classify origin remote as:
      - ("github", "owner/repo", raw_url)
      - ("local_path", None, raw_url)      # local filesystem bare repo or file://
      - ("unknown_remote", None, raw_url)  # unrecognized remote host/alias -> fail closed unless --allow-branch-only!
      - ("none", None, "")
    """
    res = run_cmd(git_cmd("remote", "get-url", "origin"), cwd=repo_root, env_role="orchestrator")
    if res.returncode != 0 or not res.stdout.strip():
        return "none", None, ""
    raw_url = res.stdout.strip()
    gh = parse_github_owner_repo_url(raw_url)
    push_urls = run_cmd(git_cmd("remote", "get-url", "--push", "--all", "origin"), cwd=repo_root,
                        env_role="orchestrator", check=True, cap_output=False).stdout.splitlines()
    if len(push_urls) != 1 or not (
        push_urls[0] == raw_url or (gh and (parse_github_owner_repo_url(push_urls[0]) or "").lower() == gh.lower())
    ):
        raise RuntimeError("Origin fetch and push destinations differ or multiple push destinations are configured.")
    if gh:
        return "github", gh, raw_url
    if raw_url.startswith("file://"):
        return "local_path", None, raw_url
    if raw_url.startswith(("/", "./", "../")) or (repo_root / raw_url).exists():
        return "local_path", None, str((repo_root / raw_url).resolve())
    return "unknown_remote", None, raw_url


def get_remote_branch_sha(repo_root: Path, branch: str, *, ignore_shutdown: bool = False) -> str:
    """Return 40-char remote SHA for origin/refs/heads/<branch>, or '' if branch does not exist remotely."""
    if run_cmd(git_cmd("remote", "get-url", "origin"), cwd=repo_root, env_role="orchestrator", ignore_shutdown=ignore_shutdown).returncode != 0:
        return ""
    res = run_cmd(
        git_cmd("ls-remote", "origin", f"refs/heads/{branch}"),
        cwd=repo_root,
        env_role="orchestrator",
        ignore_shutdown=ignore_shutdown,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git ls-remote origin {branch} failed: {redact_secrets(res.stderr)}")
    line = res.stdout.strip().splitlines()
    return line[0].split()[0] if line else ""


def check_remote_base(repo_root: Path, branch: str, expected_sha: str, phase: str) -> None:
    current_sha = get_remote_branch_sha(repo_root, branch)
    if not current_sha:
        raise RuntimeError(f"Remote base origin/{branch} was deleted during {phase}.")
    if current_sha == expected_sha:
        return
    run_cmd(git_cmd("fetch", "origin", f"refs/heads/{branch}"), cwd=repo_root, check=True, env_role="orchestrator")
    ancestry = run_cmd(git_cmd("merge-base", "--is-ancestor", expected_sha, current_sha),
                       cwd=repo_root, env_role="orchestrator")
    if ancestry.returncode != 0:
        raise RuntimeError(f"Remote base origin/{branch} was rewritten during {phase} "
                           f"({expected_sha[:8]} is no longer an ancestor of {current_sha[:8]}).")


def _snapshot_shared_git_config(worktree: Path) -> str:
    """Hash effective local git config and raw shared <git-common-dir>/config bytes."""
    cfg_res = run_cmd(git_cmd("config", "--local", "--list"), cwd=worktree, env_role="orchestrator", cap_output=False)
    cfg_list = cfg_res.stdout if cfg_res.returncode == 0 else ""
    raw_cfg_sha = ""
    try:
        cfg_file = get_git_common_dir(worktree) / "config"
        if cfg_file.is_file():
            raw_cfg_sha = sha256_file(cfg_file)
    except Exception:
        pass
    return sha256_text(f"{cfg_list}\x00{raw_cfg_sha}")


def snapshot_local_git_config(worktree: Path) -> dict[str, str]:
    """Snapshot local git config and .gitattributes hash to detect worker tampering (filter.clean, pushurl, sshCommand)."""
    attr_path = worktree / ".gitattributes"
    return {
        "local_config_sha": _snapshot_shared_git_config(worktree),
        "gitattributes_sha": sha256_file(attr_path) if attr_path.is_file() else "",
    }


def check_dirty_docs_in_main_repo(repo_root: Path, base_branch: str) -> list[str]:
    """Flag ONLY uncommitted local doc changes or locally unpushed commits (includes monorepo **/GLOSSARY.md)."""
    issues: list[str] = []
    doc_paths = ["GLOSSARY.md", ":(glob)**/GLOSSARY.md", "GLOSSARY-MAP.md", "CODING_STANDARDS.md", "docs/adr"]
    st = run_cmd(git_cmd("status", "--porcelain", "--", *doc_paths), cwd=repo_root, env_role="orchestrator").stdout.strip()
    if st:
        issues.append(f"Uncommitted domain/standard docs in main repo:\n{st}")
    if run_cmd(git_cmd("rev-parse", "--verify", f"refs/remotes/origin/{base_branch}"), cwd=repo_root, env_role="orchestrator").returncode == 0:
        unpushed = run_cmd(
            git_cmd("log", "--oneline", f"origin/{base_branch}..HEAD", "--", *doc_paths),
            cwd=repo_root,
            env_role="orchestrator",
        ).stdout.strip()
        if unpushed:
            issues.append(
                f"Unpushed domain/standard doc commits in main repo (worktrees branch from origin/{base_branch}):\n{unpushed}"
            )
    return issues


def ensure_dispatch_excluded(worktree: Path, *, ignore_shutdown: bool = False) -> None:
    res = run_cmd(git_cmd("rev-parse", "--git-path", "info/exclude"), cwd=worktree, env_role="orchestrator", ignore_shutdown=ignore_shutdown)
    if res.returncode != 0:
        return
    exclude_path = Path(res.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = (worktree / exclude_path).resolve()
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
    if ".dispatch/" not in existing.splitlines():
        with exclude_path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(".dispatch/\n")


def write_worktree_owner(worktree: Path, identifier: str, issue_uuid: str, repo_identity: str, branch: str) -> None:
    """Persist worktree lock-owner metadata inside repo lock so cleanup can NEVER race before first checkpoint."""
    owner_path = worktree / ".dispatch" / "owner.json"
    if worktree.is_symlink() or owner_path.parent.is_symlink() or owner_path.is_symlink():
        raise RuntimeError("Symlink in worktree ownership path; refusing to write metadata.")
    atomic_write_text(
        owner_path,
        json.dumps(
            {
                "issue": identifier,
                "issue_uuid": issue_uuid,
                "repo_identity": repo_identity,
                "branch": branch,
            },
            indent=2,
            ensure_ascii=False,
        ),
    )


def read_worktree_lock_owner(repo_root: Path, rkey: str, wt: Path) -> tuple[str, str, dict[str, Any], str]:
    """
    Resolve (issue_uuid, expected_branch, latest_ckpt_dict, error_reason) from owner.json (authoritative, written inside repo lock)
    and latest.json. Never crashes on corrupt JSON.
    """
    expected_repo_id = canonical_repo_identity(repo_root)
    expected_branch = f"feat/{wt.name.lower()}"
    owner_f = wt / ".dispatch" / "owner.json"
    if wt.is_symlink() or owner_f.parent.is_symlink() or owner_f.is_symlink():
        return "", "", {}, "symlink_in_owner_path"

    records = []
    checkpoint = {}
    for path, kind in (
        (owner_f, "owner"),
        (_cache_root() / "runs" / rkey / wt.name / "latest.json", "checkpoint"),
        (_cache_root() / "runs" / rkey / wt.name / "latest-local.json", "local_checkpoint"),
    ):
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", "", {}, f"corrupt_{kind}_json"
        if not isinstance(record, dict) or not record.get("issue_uuid"):
            return "", "", {}, f"missing_{kind}_metadata"
        if record.get("repo_identity") != expected_repo_id or record.get("issue") != wt.name:
            return "", "", {}, f"{kind}_ownership_mismatch"
        if record.get("branch") != expected_branch:
            return "", "", {}, f"{kind} branch ownership mismatch"
        if kind != "owner" and Path(record.get("worktree") or "").absolute() != wt.absolute():
            return "", "", {}, f"{kind}_worktree_mismatch"
        records.append(record)
        if kind == "checkpoint" or (kind == "local_checkpoint" and not checkpoint):
            checkpoint = record

    if not records:
        return "", "", {}, "missing_lock_owner_metadata"
    uuid = records[0]["issue_uuid"]
    if any(r["issue_uuid"] != uuid for r in records):
        return "", "", checkpoint, "owner_checkpoint_uuid_mismatch"
    return uuid, expected_branch, checkpoint, ""


def prepare_worktree(
    repo_root: Path,
    identifier: str,
    base_branch: str,
    *,
    fresh: bool = False,
    issue_uuid: str = "",
) -> tuple[Path, str, str]:
    """Create or safely reuse an isolated worktree under repo flock; writes owner.json before releasing repo lock."""
    rkey = repo_cache_key(repo_root)
    repo_id = canonical_repo_identity(repo_root)
    branch = f"feat/{identifier.lower()}"
    repo_common = get_git_common_dir(repo_root)

    with file_lock(_cache_root() / "locks" / f"repo-{rkey}.lock", blocking=True):
        run_cmd(git_cmd("worktree", "prune"), cwd=repo_root, env_role="orchestrator")

        base_ref = fetch_base_ref(repo_root, base_branch)
        base_sha = run_cmd(git_cmd("rev-parse", base_ref), cwd=repo_root, check=True, env_role="orchestrator").stdout.strip()

        wt_dir = _worktree_cache_path(rkey, identifier.upper())
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        if wt_dir.exists():
            if not (wt_dir / ".git").exists():
                raise RuntimeError(f"Non-git directory occupies worktree path {wt_dir}; inspect and remove manually.")
            wt_common = get_git_common_dir(wt_dir)
            if wt_common != repo_common:
                raise RuntimeError(f"Worktree ownership mismatch at {wt_dir}: belongs to {wt_common}, not {repo_common}")
            cur_br = run_cmd(git_cmd("rev-parse", "--abbrev-ref", "HEAD"), cwd=wt_dir, check=True, env_role="orchestrator").stdout.strip()
            if cur_br != branch:
                raise RuntimeError(f"Worktree {wt_dir} is on branch '{cur_br}', expected '{branch}'")
            if issue_uuid:
                owner_uuid, owner_branch, _, error = read_worktree_lock_owner(repo_root, rkey, wt_dir)
                if error or owner_uuid != issue_uuid or owner_branch != branch:
                    raise RuntimeError(f"Worktree ownership mismatch at {wt_dir}: {error or 'issue or branch changed'}")
            if fresh:
                run_cmd(git_cmd("worktree", "remove", "--force", str(wt_dir)), cwd=repo_root, check=True, env_role="orchestrator")
                run_cmd(git_cmd("branch", "-D", branch), cwd=repo_root, env_role="orchestrator")
            else:
                dirty = run_cmd(git_cmd("status", "--porcelain"), cwd=wt_dir, env_role="orchestrator").stdout.strip()
                if dirty:
                    raise RuntimeError(
                        f"Existing worktree {wt_dir} has uncommitted changes; pass --fresh to recreate cleanly:\n{dirty}"
                    )
                ensure_dispatch_excluded(wt_dir)
                if issue_uuid:
                    write_worktree_owner(wt_dir, identifier, issue_uuid, repo_id, branch)
                return wt_dir, branch, base_sha

        branch_exists = run_cmd(git_cmd("rev-parse", "--verify", f"refs/heads/{branch}"), cwd=repo_root, env_role="orchestrator").returncode == 0
        if branch_exists and fresh:
            run_cmd(git_cmd("branch", "-D", branch), cwd=repo_root, check=True, env_role="orchestrator")
            branch_exists = False

        if branch_exists:
            run_cmd(git_cmd("worktree", "add", str(wt_dir), branch), cwd=repo_root, check=True, env_role="orchestrator")
            ensure_dispatch_excluded(wt_dir)
            if issue_uuid:
                write_worktree_owner(wt_dir, identifier, issue_uuid, repo_id, branch)
            return wt_dir, branch, base_sha

        run_cmd(git_cmd("worktree", "add", "-b", branch, str(wt_dir), base_sha), cwd=repo_root, check=True, env_role="orchestrator")
        ensure_dispatch_excluded(wt_dir)
        if issue_uuid:
            write_worktree_owner(wt_dir, identifier, issue_uuid, repo_id, branch)
        return wt_dir, branch, base_sha


def check_remote_fast_forward_ready(repo_root: Path, worktree: Path, branch: str) -> str:
    """Fail fast BEFORE starting a long worker if origin/<branch> already diverged from local HEAD."""
    remote_sha = get_remote_branch_sha(repo_root, branch)
    if not remote_sha:
        return ""
    run_cmd(git_cmd("fetch", "origin", f"refs/heads/{branch}"), cwd=repo_root, check=True, env_role="orchestrator")
    local_head = run_cmd(git_cmd("rev-parse", "HEAD"), cwd=worktree, check=True, env_role="orchestrator").stdout.strip()
    anc = run_cmd(git_cmd("merge-base", "--is-ancestor", remote_sha, local_head), cwd=worktree, env_role="orchestrator")
    if anc.returncode != 0:
        raise RuntimeError(
            f"Remote branch origin/{branch} ({remote_sha[:8]}) has diverged from local HEAD ({local_head[:8]}). "
            f"Rebase/reconcile manually before dispatching to avoid post-run push rejection."
        )
    return remote_sha


def tracked_status(worktree: Path) -> tuple[list[str], list[str]]:
    status = run_cmd(git_cmd("status", "--porcelain=v2", "--branch", "--untracked-files=no",
                             "--no-renames", "-z"),
                     cwd=worktree, check=True, env_role="orchestrator", cap_output=False)
    records = list(filter(None, status.stdout.split("\0")))
    return [r for r in records if r.startswith("# branch.")], [r for r in records if not r.startswith("# ")]


def bootstrap_worktree(worktree: Path, setup_cmd_override: str | None, *, timeout: int = 600) -> dict[str, Any]:
    """Install dependencies for fresh worktree (supports mixed Node + Python repos)."""
    cmds: list[str] = []
    if setup_cmd_override:
        cmds.append(setup_cmd_override)
    else:
        if (worktree / "package.json").is_file() and not (worktree / "node_modules").exists():
            if (worktree / "pnpm-lock.yaml").is_file() and shutil.which("pnpm"):
                cmds.append("pnpm install --frozen-lockfile")
            elif ((worktree / "bun.lockb").is_file() or (worktree / "bun.lock").is_file()) and shutil.which("bun"):
                cmds.append("bun install --frozen-lockfile")
            elif (worktree / "yarn.lock").is_file() and shutil.which("yarn"):
                cmds.append("yarn install --frozen-lockfile")
            elif (worktree / "package-lock.json").is_file() and shutil.which("npm"):
                cmds.append("npm ci")
        if (worktree / "uv.lock").is_file() and not (worktree / ".venv").exists() and shutil.which("uv"):
            cmds.append("uv sync --frozen")

    if not cmds:
        return {"ran": False, "commands": []}
    branch, dirty = tracked_status(worktree)
    if dirty:
        raise RuntimeError("Bootstrap requires a clean index and tracked files.")
    before_git_cfg = _snapshot_shared_git_config(worktree)
    for c in cmds:
        log(f"Bootstrapping worktree dependencies: {c}")
        run_cmd(
            c,
            cwd=worktree,
            shell=True,
            check=True,
            timeout=timeout,
            env_role="child",
            extra_env={"HUSKY": "0", "HUSKY_SKIP_INSTALL": "1"},
        )
        if _snapshot_shared_git_config(worktree) != before_git_cfg:
            raise RuntimeError(f"Bootstrap modified shared git config (.git/config) in {worktree}; refusing to proceed.")
    after, dirty = tracked_status(worktree)
    if after != branch or dirty:
        raise RuntimeError(f"Bootstrap changed the branch, HEAD or tracked files in {worktree}.")
    return {"ran": True, "commands": cmds}


def verify_worktree_branch_integrity(worktree: Path, expected_common_dir: Path, expected_branch: str, base_sha: str) -> None:
    """Guard against worker switching branches or rewriting history away from base_sha before orchestrator commit."""
    actual_common = get_git_common_dir(worktree)
    if actual_common != expected_common_dir:
        raise RuntimeError(f"Worktree common-dir mismatch after worker: {actual_common} != {expected_common_dir}")
    actual_branch = run_cmd(git_cmd("rev-parse", "--abbrev-ref", "HEAD"), cwd=worktree, check=True, env_role="orchestrator").stdout.strip()
    if actual_branch != expected_branch:
        raise RuntimeError(f"Worker switched branch from '{expected_branch}' to '{actual_branch}'; refusing to commit.")
    anc = run_cmd(git_cmd("merge-base", "--is-ancestor", base_sha, "HEAD"), cwd=worktree, env_role="orchestrator")
    if anc.returncode != 0:
        raise RuntimeError(f"Worker rewrote git history so base_sha {base_sha[:8]} is no longer an ancestor of HEAD.")
