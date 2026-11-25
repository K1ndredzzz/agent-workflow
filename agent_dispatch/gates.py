"""Verification, baseline caching, policy gates, and Git secret scanning."""

from __future__ import annotations

import ast
import codecs
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Iterator

from agent_dispatch import config
from agent_dispatch.brief import path_matches_any_pattern
from agent_dispatch.git_ops import bootstrap_worktree, ensure_dispatch_excluded, tracked_status
from agent_dispatch.process import atomic_write_text, file_lock, git_cmd, log, run_cmd, sha256_text
from agent_dispatch.secrets import is_secret_filename, redact_secrets, scan_text_for_secrets


def _test_source_files(worktree: Path) -> Iterator[Path]:
    for root, dirs, names in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}]
        for name in names:
            if (name.endswith(".py") and (name.startswith("test") or name.endswith("_test.py"))) or re.search(
                r"\.(?:test|spec)\.[cm]?[jt]sx?$", name
            ):
                yield Path(root) / name


def _unittest_can_discover(tests_dir: Path, files: list[Path]) -> bool:
    for path in files:
        if not path.is_relative_to(tests_dir) or not path.name.startswith("test"):
            return False
        if any(not (p / "__init__.py").is_file() for p in path.parents if p.is_relative_to(tests_dir) and p != tests_dir):
            return False
        try:
            module = ast.parse(path.read_bytes(), filename=str(path))
        except (SyntaxError, ValueError):
            return False
        case_types = {"unittest.TestCase", "unittest.IsolatedAsyncioTestCase"}
        for node in module.body:
            if isinstance(node, ast.ImportFrom) and node.module == "unittest":
                case_types.update(a.asname or a.name for a in node.names if "unittest." + a.name in case_types)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "unittest" and alias.asname:
                        case_types.update({alias.asname + ".TestCase", alias.asname + ".IsolatedAsyncioTestCase"})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                return False
            if isinstance(node, ast.ClassDef):
                if any(ast.unparse(base) in case_types for base in node.bases):
                    case_types.add(node.name)
                elif node.name.startswith("Test"):
                    return False
        if any(isinstance(n, ast.ImportFrom) and n.module == "pytest" or
               isinstance(n, ast.Import) and any(a.name == "pytest" for a in n.names) for n in ast.walk(module)):
            return False
    return bool(files)


def _repo_has_js_tests(worktree: Path) -> bool:
    if any(p.suffix != ".py" for p in _test_source_files(worktree)):
        return True
    for d in ("tests", "test", "__tests__"):
        root = worktree / d
        if not root.is_dir():
            continue
        files = [p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        # Empty test dir or any non-Python file signals possible JS tests;
        # a tests/ dir containing exclusively Python files belongs to Python.
        if not files or any(p.suffix not in (".py", ".pyc") for p in files):
            return True
    return False


def detect_verify_command(worktree: Path, explicit_cmd: str | None) -> str | None:
    if explicit_cmd:
        return explicit_cmd

    just_params = r"(?:\s+[^:=\n]*)?"
    for filename, tool, params in (
        ("justfile", "just", just_params),
        ("Justfile", "just", just_params),
        ("Makefile", "make", r"\s*"),
    ):
        recipe = worktree / filename
        if recipe.is_file():
            text = recipe.read_text(encoding="utf-8", errors="ignore")
            for target in ("verify", "check"):
                if re.search(rf"^{target}{params}:(?!=)", text, re.M):
                    return f"{tool} {target}"

    commands: list[str] = []
    pkg = worktree / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
            if (worktree / "pnpm-lock.yaml").is_file():
                pm = "pnpm"
            elif (worktree / "bun.lockb").is_file() or (worktree / "bun.lock").is_file():
                pm = "bun run"
            elif (worktree / "yarn.lock").is_file():
                pm = "yarn"
            else:
                pm = "npm run"
            js_cmds = [f"{pm} {s}" for s in ("typecheck", "lint", "test") if s in scripts]
            # Never pass lint-only JS repo if JS test files/dirs exist without a `test` script!
            if "test" not in scripts and _repo_has_js_tests(worktree):
                return None
            commands.extend(js_cmds)
        except (OSError, ValueError, AttributeError, TypeError):
            return None

    if (worktree / "Cargo.toml").is_file():
        commands.extend(["cargo clippy --all-targets", "cargo test"])

    python_config = (worktree / "pyproject.toml").is_file() or (worktree / "pytest.ini").is_file()
    py_tests = [p for p in _test_source_files(worktree) if p.suffix == ".py"]
    if python_config or py_tests:
        if python_config and shutil.which("ruff"):
            commands.append("ruff check .")
        if (worktree / ".venv" / "bin" / "pytest").is_file():
            commands.append(".venv/bin/pytest")
        elif (worktree / "uv.lock").is_file() and shutil.which("uv"):
            commands.append("uv run pytest")
        elif not python_config and _unittest_can_discover(worktree / "tests", py_tests):
            commands.append("python3 -m unittest discover -s tests")
        elif shutil.which("pytest"):
            commands.append("pytest")
        elif _unittest_can_discover(worktree / "tests", py_tests):
            commands.append("python3 -m unittest discover -s tests")
        elif py_tests or (worktree / "tests").is_dir() or (worktree / "pytest.ini").is_file():
            return None

    if (worktree / "go.mod").is_file():
        commands.extend(["go vet ./...", "go test ./..."])

    return " && ".join(commands) or None


def untracked_files(worktree: Path, *, ignore_shutdown: bool = False) -> set[str]:
    return set(_untracked_paths(worktree, ignore_shutdown=ignore_shutdown))


def _untracked_paths(worktree: Path, *, ignore_shutdown: bool) -> Iterator[str]:
    listing = _git(
        worktree, "ls-files", "--others", "--exclude-standard", "-z", cap_output=False, ignore_shutdown=ignore_shutdown,
    ).stdout
    return filter(None, listing.split("\0"))


def file_identity(path: Path) -> tuple[int, int, int, int]:
    st = path.lstat()
    return st.st_dev, st.st_ino, st.st_ctime_ns, st.st_size


def release_setup_files(worktree: Path, files: dict[str, tuple[int, int, int, int]]) -> None:
    if not files:
        return
    try:
        wt_resolved = worktree.resolve()
        for name in files.keys() & untracked_files(worktree, ignore_shutdown=True):
            path = worktree / name
            if path.parent.resolve().is_relative_to(wt_resolved) and (path.is_file() or path.is_symlink()):
                if file_identity(path) == files[name]:
                    path.unlink()
    except (OSError, RuntimeError) as exc:
        log(f"Could not release setup output: {redact_secrets(str(exc))}")


def run_verify_gate(
    worktree: Path, verify_cmd: str | None, *,
    allow_no_verify: bool,
    log_file: Path,
    repo_key: str,
    timeout: int = 900,
) -> dict[str, Any]:
    if not verify_cmd:
        if allow_no_verify:
            rc, output = 0, "Bypassed via --allow-no-verify."
        else:
            rc, output = 1, "No verification command detected. Pass --verify-cmd or configure a repo check script (or --allow-no-verify)."
        return {"ran": False, "passed": rc == 0, "command": None, "returncode": rc, "output": output}
    log(f"Running verification gate: {verify_cmd}")
    with file_lock(config._cache_root() / "locks" / f"verify-{repo_key}.lock", blocking=True):
        original, dirty_before = tracked_status(worktree)
        if dirty_before:
            raise RuntimeError("Verification requires tracked files to match the candidate commit. Existing edits were preserved.")
        prior_files = untracked_files(worktree)
        try:
            res = run_cmd(
                verify_cmd, cwd=worktree, shell=True, timeout=timeout, env_role="child",
                stdout_path=log_file, stderr_path=log_file.with_suffix(".stderr.log"),
            )
            rc = res.returncode
            output = (res.stdout + "\n" + res.stderr).strip()
        except subprocess.TimeoutExpired:
            rc = 124
            output = f"Verification timed out after {timeout}s (see {log_file})."

        branch, dirty_after = tracked_status(worktree)
        if branch != original:
            raise RuntimeError("Verification changed the worktree branch or HEAD. Inspect the worktree before rerunning.")
        passed = rc == 0
        if dirty_after:
            _git(worktree, "reset", "--hard", "HEAD")
            if passed:
                passed = False
                output = "Verification mutated tracked files: " + repr(dirty_after[:5])
        added = sorted(untracked_files(worktree) - prior_files)
        for offset in range(0, len(added), 100):
            _git(worktree, "--literal-pathspecs", "clean", "-fd", "-e", ".dispatch", "--", *added[offset:offset + 100])
        if passed and "unittest discover" in verify_cmd and re.search(r"^Ran 0 tests? in ", output, re.M):
            passed = False
            output = "unittest discovery collected zero tests."
        return {"ran": True, "passed": passed, "command": verify_cmd, "returncode": rc,
                "output": redact_secrets(output)[-4000:]}


def _git(
    worktree: Path, *args: str, attr_source: str | None = None, check: bool = True, **options: Any,
) -> subprocess.CompletedProcess[str]:
    return run_cmd(git_cmd(*args, attr_source=attr_source), cwd=worktree, check=check, env_role="orchestrator", **options)


def _git_head(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def check_base_baseline(
    repo_root: Path, repo_key: str, worktree: Path, base_sha: str, verify_cmd: str | None, run_dir: Path, *,
    setup_cmd_override: str | None = None,
    setup_timeout: int = 600,
    allow_no_verify: bool = False,
    timeout: int = 900,
) -> dict[str, Any]:
    baselines_file = config._cache_root() / "baselines.json"
    baselines_lock = config._cache_root() / "locks" / "baselines.lock"
    lease_key = sha256_text(json.dumps([repo_key, base_sha, verify_cmd, setup_cmd_override]))
    key_lock = config._cache_root() / "locks" / f"baseline-{lease_key[:16]}.lock"

    def read_cache() -> dict[str, bool]:
        try:
            cache = json.loads(baselines_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return cache if isinstance(cache, dict) else {}

    def cached_success(key: str) -> bool:
        with file_lock(baselines_lock, blocking=True):
            return read_cache().get(key) is True

    with file_lock(key_lock, blocking=True):
        # The explicit command already identifies the recipe and its existing cache entries.
        cache_key = lease_key
        if verify_cmd and cached_success(cache_key):
            return {"checked": True, "cached": True, "passed": True}

        target_wt = worktree
        temp_wt: Path | None = None
        head = _git_head(worktree)
        try:
            if head != base_sha:
                # Reused feature branch with baseline cache miss: verify base_sha in a UNIQUE temporary detached worktree!
                temp_wt = config._worktree_cache_path(repo_key, f".baseline-{base_sha[:10]}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
                owner_file = config._cache_root() / "baseline-owners" / repo_key / temp_wt.name
                with file_lock(config._cache_root() / "locks" / f"repo-{repo_key}.lock", blocking=True):
                    atomic_write_text(owner_file, json.dumps({"key": lease_key, "base_sha": base_sha}))
                    _git(repo_root, "worktree", "add", "--detach", str(temp_wt), base_sha)
                bootstrap_worktree(temp_wt, setup_cmd_override, timeout=setup_timeout)
                target_wt = temp_wt

            if _git_head(target_wt) != base_sha:
                raise RuntimeError("Bootstrap changed the baseline HEAD before verification")
            if not verify_cmd:
                verify_cmd = detect_verify_command(target_wt, None)
                cache_key = sha256_text(json.dumps([lease_key, verify_cmd]))
                if cached_success(cache_key):
                    return {"checked": True, "cached": True, "passed": True}
            log(f"Checking clean base baseline ({base_sha[:8]}): {verify_cmd}")
            res = run_verify_gate(
                target_wt, verify_cmd, allow_no_verify=allow_no_verify,
                log_file=run_dir / "baseline-verify.log", repo_key=repo_key, timeout=timeout,
            )
        finally:
            if temp_wt is not None:
                with file_lock(config._cache_root() / "locks" / f"repo-{repo_key}.lock", ignore_shutdown=True):
                    config._worktree_cache_path(repo_key, temp_wt.name)
                    _git(repo_root, "worktree", "remove", "--force", str(temp_wt), check=False, ignore_shutdown=True)
                    if not temp_wt.exists():
                        owner_file.unlink(missing_ok=True)
                    else:
                        log(f"Baseline worktree still registered for cleanup: {temp_wt}")

        if res["passed"] and res["ran"]:
            with file_lock(baselines_lock, blocking=True):
                cache = read_cache()
                cache[cache_key] = True
                atomic_write_text(baselines_file, json.dumps(cache, indent=2))
        return {"checked": True, "cached": False, **res}


def _policy_diff(worktree: Path, base_sha: str, *args: str, literal_pathspecs: bool = False) -> str:
    command = ["--literal-pathspecs", "diff"] if literal_pathspecs else ["diff"]
    return _git(
        worktree, *command, "--no-renames", "--no-ext-diff", "--no-textconv", *args, attr_source=base_sha, cap_output=False,
    ).stdout


def check_protected_paths(
    worktree: Path, base_sha: str, candidate_sha: str, *,
    allowed_patterns: list[str] | None = None,
    allow_all: bool = False,
) -> list[str]:
    """Use --no-renames -z (NUL-safe for non-ASCII/special paths); supports granular allowlist patterns."""
    if allow_all:
        return []
    raw = _policy_diff(worktree, base_sha, "-z", "--name-only", f"{base_sha}..{candidate_sha}")
    violations: list[str] = []
    for f in filter(None, raw.split("\0")):
        if allowed_patterns and path_matches_any_pattern(f, allowed_patterns):
            continue
        base_name = Path(f).name
        if (
            f in config.PROTECTED_EXACT_FILES
            or base_name in config.PROTECTED_BASENAMES
            or any("/" + p in "/" + f for p in config.PROTECTED_PREFIXES)
            or any(r.search(f) for r in config.PROTECTED_REGEXES)
        ):
            violations.append(f"Protected gate/policy file modified: {f}")
        elif base_name == "package.json":
            old_raw, new_raw = (
                _git(worktree, "show", f"{sha}:{f}", check=False, cap_output=False)
                for sha in (base_sha, candidate_sha)
            )
            try:
                new_pkg = json.loads(new_raw.stdout) if new_raw.returncode == 0 else {}
            except json.JSONDecodeError as exc:
                violations.append(f"Invalid JSON in `{f}`: {exc}")
                continue
            if not isinstance(new_pkg, dict):
                violations.append(f"Invalid JSON object in `{f}`")
                continue
            sections = ("scripts", "jest", "vitest", "eslintConfig")
            if old_raw.returncode == 0:
                try:
                    old_pkg = json.loads(old_raw.stdout)
                except json.JSONDecodeError:
                    old_pkg = {}
                for section in sections:
                    old_value = old_pkg.get(section) if isinstance(old_pkg, dict) else None
                    if old_value != new_pkg.get(section):
                        violations.append(f"Protected `{f}` `{section}` section modified")
            elif any(new_pkg.get(s) is not None for s in sections):
                # Workspace packages can introduce gate scripts too.
                violations.append(f"Protected `{f}` gate/script config added")
    return violations


def check_test_integrity_diff(
    worktree: Path, base_sha: str, candidate_sha: str, *,
    allowed_patterns: list[str] | None = None,
    allow_all: bool = False,
) -> list[str]:
    """Deterministically detect deleted test files or added skip/only/ignore markers (NUL-safe, granular allowlist)."""
    if allow_all:
        return []
    findings: list[str] = []
    revision_range = f"{base_sha}..{candidate_sha}"
    raw_status = _policy_diff(worktree, base_sha, "-z", "--name-status", revision_range)
    test_files: list[str] = []
    # With renames disabled, each record contains a status and one path.
    fields = filter(None, raw_status.split("\0"))
    for st, path in zip(fields, fields):
        if allowed_patterns and path_matches_any_pattern(path, allowed_patterns):
            continue
        if st.startswith("D") and config.TEST_FILE_RE.search(path):
            findings.append(f"Deleted test file: {path}")
        elif config.TEST_FILE_RE.search(path):
            test_files.append(path)

    for tf in test_files:
        diff_txt = _policy_diff(worktree, base_sha, "--text", "--no-color", "-U0", revision_range, "--", tf,
                                literal_pathspecs=True)
        for added in _added_patch_lines(diff_txt):
            if any(pat.search(added) for pat in config.TEST_SKIP_PATTERNS):
                findings.append(f"Added test skip/only/ignore marker in {tf}: {redact_secrets(added.strip())[:120]}")
    return findings


def _added_patch_lines(patch: str) -> Iterator[str]:
    # Git headers are outside hunks. A source line may itself begin with '++'.
    in_hunk = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            yield line[1:]


def _git_text_views(data: bytes) -> Iterator[str]:
    yield data.decode("utf-8", errors="replace")
    if not re.search(rb"(?:[ -~]\x00){3}|(?:\x00[ -~]){3}", data):
        return
    # Git splits at the LF byte, including inside UTF-16 code units. Try each initial
    # alignment and carry it across the physical lines.
    lines = data.split(b"\n")
    for encoding in ("utf-16-le", "utf-16-be"):
        for offset in (0, 1):
            decoded = []
            for line in lines:
                end = len(line) - (len(line) - offset) % 2
                decoded.append(line[offset:end].decode(encoding, errors="replace").lstrip("\ufeff"))
                offset = (offset + len(line) + 1) % 2
            yield "\n".join(decoded)


def _scan_git_bytes(data: bytes, field: str, path: str) -> list[str]:
    for text in _git_text_views(data):
        hits = scan_text_for_secrets(text, field)
        if hits:
            redacted_path = redact_secrets(path)
            return [f"{hit} ({redacted_path})" for hit in hits]
    return []


def _scan_git_changes_for_secrets(
    worktree: Path,
    command: list[str],
    *,
    ignore_shutdown: bool = False,
    attr_source: str | None = None,
) -> list[str]:
    flags = ["--no-renames", "--no-ext-diff", "--no-textconv", "--no-color"]
    if not attr_source:
        head = _git(worktree, "rev-parse", "--verify", "-q", "HEAD", check=False, ignore_shutdown=ignore_shutdown)
        attr_source = "HEAD" if head.returncode == 0 else None

    paths = _git(
        worktree, *command, *flags, "-z", "--diff-filter=AMT", "--name-only",
        attr_source=attr_source, cap_output=False, ignore_shutdown=ignore_shutdown,
    ).stdout
    findings: set[str] = set()
    for path in set(paths.split("\0")) - {""}:
        findings.update(scan_text_for_secrets(path, "changed file name", allow_marker=False))
        if is_secret_filename(path):
            findings.add(f"Potential secret file: {redact_secrets(path)}")

    # --text scans content even when Git would hide the patch because of NUL or attributes.
    patch = _git(
        worktree, *command, *flags, "-p", "--text", "--no-prefix", "-U0",
        attr_source=attr_source, cap_output=False, stdout_encoding="latin-1", ignore_shutdown=ignore_shutdown,
    ).stdout
    for block in ("\n" + patch).split("\ndiff --git ")[1:]:
        added = "\n".join(_added_patch_lines(block)).encode("latin-1")
        if not added:
            continue
        # The destination header contains one path. Git C-quotes special characters
        # and may append a tab to separate the path from an omitted timestamp.
        header = block.partition("\n+++ ")[2].partition("\n")[0].removesuffix("\t")
        path_bytes = header.encode("latin-1")
        if path_bytes.startswith(b'"'):
            path_bytes = codecs.escape_decode(path_bytes[1:-1])[0]
        path = path_bytes.decode("utf-8", errors="replace")
        findings.update(_scan_git_bytes(added, "added file content", path))
    return sorted(findings)


def scan_range_for_secrets(worktree: Path, base_sha: str, candidate_sha: str) -> list[str]:
    revision_range = f"{base_sha}..{candidate_sha}"
    messages = _git(worktree, "log", "--pretty=format:%B%n", revision_range, cap_output=False).stdout
    return scan_text_for_secrets(messages, "commit message", allow_marker=False) + _scan_git_changes_for_secrets(
        worktree, ["log", "-m", "--format=", revision_range], attr_source=base_sha
    )


def scan_staged_for_secrets(worktree: Path, *, attr_source: str | None = None) -> list[str]:
    return _scan_git_changes_for_secrets(worktree, ["diff", "--cached"], attr_source=attr_source)


def scan_before_git_write(
    worktree: Path,
    *,
    for_stash: bool = False,
    exclude_paths: set[str] | frozenset[str] = frozenset(),
    attr_source: str | None = None,
) -> list[str]:
    # stash -u records three trees: index, tracked working copy, and untracked files.
    findings = _scan_git_changes_for_secrets(worktree, ["diff", "HEAD"], ignore_shutdown=for_stash, attr_source=attr_source)
    if for_stash:
        findings.extend(_scan_git_changes_for_secrets(worktree, ["diff", "--cached"], ignore_shutdown=True, attr_source=attr_source))
    for name in _untracked_paths(worktree, ignore_shutdown=for_stash):
        if name in exclude_paths:
            continue
        findings.extend(scan_text_for_secrets(name, "untracked file name", allow_marker=False))
        if is_secret_filename(name):
            findings.append(f"Potential secret file: {redact_secrets(name)}")
            continue
        path = worktree / name
        if path.is_symlink():
            try:
                content = os.readlink(path)
            except OSError:
                findings.append(f"Unable to read untracked symlink: {redact_secrets(name)}")
                continue
            if is_secret_filename(content):
                findings.append(f"Potential secret symlink target: {redact_secrets(name)}")
                continue
            findings.extend(scan_text_for_secrets(content, "untracked symlink target", allow_marker=False))
        elif path.is_file():
            try:
                findings.extend(_scan_git_bytes(path.read_bytes(), "untracked file content", name))
            except OSError:
                findings.append(f"Unable to read untracked file for secret scan: {redact_secrets(name)}")
        else:
            findings.append("Unable to scan untracked entry as a regular file or symbolic link")
    return findings


def orchestrator_commit(
    worktree: Path, identifier: str, title: str, attempt: int, *,
    setup_paths: tuple[str, ...] = (),
    attr_source: str | None = None,
) -> tuple[str, list[str]]:
    """Stage & commit changes safely (hooks/fsmonitor disabled; scans commit msg + staged diff). Returns (head_sha, violations)."""
    ensure_dispatch_excluded(worktree)
    commit_msg = f"{identifier}: {title} (attempt {attempt})"
    msg_secret_hits = scan_text_for_secrets(commit_msg, "ticket title / commit message", allow_marker=False)
    if msg_secret_hits:
        return _git_head(worktree), msg_secret_hits
    status = _git(worktree, "status", "--porcelain").stdout.strip()
    if status:
        secret_hits = scan_before_git_write(worktree, exclude_paths=frozenset(setup_paths), attr_source=attr_source)
        if secret_hits:
            return _git_head(worktree), secret_hits
        if setup_paths:
            # Explicit literal excludes work for names containing whitespace, glob or pathspec characters.
            pathspec = "\0".join([".", *(f":(top,exclude,literal){p}" for p in setup_paths)]) + "\0"
            _git(worktree, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", stdin_text=pathspec)
            _git(
                worktree, "--literal-pathspecs", "reset", "-q", "--pathspec-from-file=-", "--pathspec-file-nul",
                stdin_text="\0".join(setup_paths) + "\0",
            )
        else:
            _git(worktree, "add", "-A")
        _git(worktree, "reset", "-q", "--", ".dispatch", check=False)
        staged = _git(worktree, "diff", "--cached", "-z", "--name-only").stdout
        if staged.strip("\x00"):
            secret_hits = scan_staged_for_secrets(worktree, attr_source=attr_source)
            if secret_hits:
                _git(worktree, "reset", "-q", check=False)
                return _git_head(worktree), secret_hits
            _git(worktree, "commit", "--no-verify", "-m", commit_msg)
    return _git_head(worktree), []
