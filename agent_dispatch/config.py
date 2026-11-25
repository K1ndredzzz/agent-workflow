"""Configurable paths, workflow rubrics, and policy registries."""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any


def _linear_url() -> str:
    return os.environ.get("AGENT_DISPATCH_LINEAR_URL", "https://api.linear.app/graphql").strip()


def _cache_root() -> Path:
    custom = os.environ.get("AGENT_DISPATCH_CACHE_ROOT", "").strip()
    return Path(custom).resolve() if custom else (Path.home() / ".cache" / "agent-dispatch")


def _worktree_cache_path(repo_key: str, name: str | None = None) -> Path:
    """Accept the configured cache root, but never traverse symlinks below it."""
    path = _cache_root()
    components = ("worktrees", repo_key) if name is None else ("worktrees", repo_key, name)
    for part in components:
        if not part or part in (".", "..") or Path(part).name != part:
            raise RuntimeError("Invalid managed worktree path component.")
        path = path / part
        if path.is_symlink():
            raise RuntimeError(f"Refusing symlink in managed worktree path: {path}")
    return path


def _resolve_skills_root() -> Path:
    custom = os.environ.get("AGENT_DISPATCH_SKILLS_ROOT", "").strip()
    if custom:
        return Path(custom).resolve()
    rel = Path(__file__).resolve().parent.parent / "skills"
    if (rel / "dispatch" / "references").is_dir():
        return rel
    return Path("/root/.agent/skills")


SKILLS_ROOT = _resolve_skills_root()
TDD_REF_PATH = SKILLS_ROOT / "dispatch" / "references" / "tdd.md"
REVIEW_STANDARDS_REF_PATH = SKILLS_ROOT / "dispatch" / "references" / "review-standards.md"
REVIEW_SPEC_REF_PATH = SKILLS_ROOT / "dispatch" / "references" / "review-spec.md"
MCP_CONFIG_PATH = Path.home() / ".gemini" / "config" / "mcp_config.json"

PRELOADED_REFS = MappingProxyType({
    "tdd": TDD_REF_PATH.read_text(encoding="utf-8") if TDD_REF_PATH.is_file() else "",
    "standards": REVIEW_STANDARDS_REF_PATH.read_text(encoding="utf-8") if REVIEW_STANDARDS_REF_PATH.is_file() else "",
    "spec": REVIEW_SPEC_REF_PATH.read_text(encoding="utf-8") if REVIEW_SPEC_REF_PATH.is_file() else "",
})


def assert_required_rubrics_loaded() -> None:
    missing = [k for k in ("tdd", "standards", "spec") if not PRELOADED_REFS.get(k, "").strip()]
    if missing:
        raise RuntimeError(
            f"Required workflow rubric files missing or empty under {SKILLS_ROOT}: {missing}. "
            f"Re-run install.sh or set AGENT_DISPATCH_SKILLS_ROOT."
        )


DISPLAY_OUTPUT_CAP = 2 * 1024 * 1024
HARNESS_OUTPUT_LIMIT = 32 * 1024 * 1024

WORKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "summary": {"type": "string"},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "discovered_issues": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "string"},
    },
    "required": ["status", "summary", "tests_run", "discovered_issues", "blockers"],
    "additionalProperties": False,
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "axis": {"type": "string", "enum": ["standards", "spec"]},
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "hard_findings": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "test_integrity_violation": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": [
        "axis",
        "verdict",
        "hard_findings",
        "suggestions",
        "test_integrity_violation",
        "summary",
    ],
    "additionalProperties": False,
}

HARNESS_ALIASES: dict[str, dict[str, str]] = {
    "codex": {
        "": "gpt-6.2-sol",
        "sol": "gpt-6.2-sol",
        "astra": "gpt-6.2-astra",
    },
    "agy": {
        "": "gemini-4-neon-xhigh",
        "neon": "gemini-4-neon-xhigh",
        "neon-xhigh": "gemini-4-neon-xhigh",
        "neon-high": "gemini-4-neon-high",
        "neon-med": "gemini-4-neon-medium",
        "neon-low": "gemini-4-neon-low",
        "carbon": "gemini-4-carbon-high",
        "carbon-high": "gemini-4-carbon-high",
        "carbon-med": "gemini-4-carbon-medium",
        "carbon-low": "gemini-4-carbon-low",
        "argon": "gemini-4-argon-high",
        "hydrogen": "gemini-4-hydrogen-xhigh",
    },
    "opencode": {
        "": "sub2api/gpt-6.2-sol",
        "sol": "sub2api/gpt-6.2-sol",
        "astra": "sub2api/gpt-6.2-astra",
    },
    "pi": {
        "": "sub2api/gpt-6.2-sol",
        "sol": "sub2api/gpt-6.2-sol",
        "astra": "sub2api/gpt-6.2-astra",
    },
}

PROTECTED_EXACT_FILES: set[str] = set()

PROTECTED_BASENAMES = {
    "justfile",
    "Justfile",
    "Makefile",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "CODING_STANDARDS.md",
    "CONTRIBUTING.md",
    "GLOSSARY.md",
    "GLOSSARY-MAP.md",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "noxfile.py",
    "mypy.ini",
    ".coveragerc",
    "ruff.toml",
    ".ruff.toml",
    ".pre-commit-config.yaml",
    ".gitattributes",
    "Cargo.toml",
    "conftest.py",
}

PROTECTED_PREFIXES = (
    "docs/adr/",
    ".github/workflows/",
    ".github/actions/",
    ".cargo/",
)

PROTECTED_REGEXES = (
    re.compile(r"(^|/)(vitest|vite|jest|eslint|playwright)\.config\..+$"),
    re.compile(r"(^|/)\.(eslintrc|mocharc)(\..+)?$"),
    re.compile(r"(^|/)tsconfig(\..+)?\.json$"),
    re.compile(r"\.mk$", re.I),
)

SECRET_FILENAME_ALLOWLIST = (
    re.compile(r"(^|/)\.env\.(example|sample|template|dist|schema)$", re.I),
)

SECRET_FILENAME_PATTERNS = (
    re.compile(r"(^|/)\.env(\..+)?$"),
    re.compile(r"\.(pem|key|p12|pfx)$", re.I),
    re.compile(r"(^|/)(id_rsa|id_ed25519|credentials\.json|secrets?\.(json|ya?ml|toml))$", re.I),
)

PEM_START = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
PEM_END = re.compile(r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")

SECRET_VALUE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])lin_api_[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9_-])gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-|live-|test-|ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])[sr]k_live_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
)

PLANNING_TITLE_PREFIXES = ("[research]", "[grill]", "[prototype]", "[map]", "[task]")

TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|"
    r"(^|/)(tests?\.py|test_[^/]+|[^/]+_(test|spec)\.[^/]+|[^/]+\.(test|spec)\.[^/]+|[^/]+Test\.[^/]+)$",
    re.I,
)

TEST_SKIP_PATTERNS = (
    re.compile(r"\b(it|test|describe|context)\.(skip|only|todo|skipIf|runIf)\s*\("),
    re.compile(r"\b(xit|xtest|xdescribe|fit|fdescribe)\s*\("),
    re.compile(r"@pytest\.mark\.(skip|skipif|xfail)\b"),
    re.compile(r"\bpytestmark\s*=.*pytest\.mark\.(skip|skipif|xfail)\b"),
    re.compile(r"\bpytest\.(skip|xfail)\s*\("),
    re.compile(r"@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b"),
    re.compile(r"\bself\.skipTest\s*\("),
    re.compile(r"#\s*\[\s*ignore\b"),
    re.compile(r"\bt\.Skip(?:Now)?\s*\("),
    re.compile(r"@(Disabled|Ignore)\b"),
)

TRUSTED_PR_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
