"""Build worker prompts and run read-only Two-Axis Review."""

from __future__ import annotations

import codecs
import concurrent.futures
import os
import subprocess
from pathlib import Path
from typing import Any

from .brief import assert_sha256_intact, parse_touches, touches_overlap
from .config import PRELOADED_REFS, PROTECTED_BASENAMES, PROTECTED_EXACT_FILES, REVIEW_SCHEMA
from .gates import file_identity, scan_range_for_secrets, untracked_files
from .harness import HarnessSpec, invoke_harness
from .process import (
    atomic_write_text,
    check_shutdown,
    git_cmd,
    interruptible_sleep,
    is_shutdown_requested,
    log,
    run_cmd,
    sha256_file,
)
from .secrets import redact_secrets


def build_worker_prompt(
    identifier: str,
    brief_path: Path,
    verify_cmd: str | None,
    *,
    allow_gate_edits: bool,
    standard_paths: list[Path],
    authorized_gate_patterns: list[str] | None = None,
    feedback: str | None = None,
) -> str:
    tdd_rules = PRELOADED_REFS["tdd"]
    verify_line = f"Run `{verify_cmd}` before finishing." if verify_cmd else "Run the repository tests and typecheck before finishing."
    protected_summary = ", ".join(sorted(PROTECTED_EXACT_FILES | PROTECTED_BASENAMES))
    if allow_gate_edits:
        gate_line = "- Gate/policy file edits are explicitly enabled via CLI flag for this ticket; keep changes minimal and justified."
    elif authorized_gate_patterns:
        pats_str = ", ".join(f"`{p}`" for p in authorized_gate_patterns)
        gate_line = (
            f"- ONLY these protected paths are authorized for editing by the ticket brief: {pats_str}. "
            f"Do NOT modify any other protected gate/policy files ({protected_summary}, `docs/adr/*`, `.github/workflows/*`, `*.mk`, or `package.json` gate sections)."
        )
    else:
        gate_line = f"- Do NOT modify protected gate/policy files ({protected_summary}, `docs/adr/*`, `.github/workflows/*`, `*.mk`, or `package.json` gate sections)."
    lines = [
        f"You are implementing Linear ticket {identifier} inside an isolated git worktree.",
        "",
        "## Required Reading First",
        f"1. Read `{brief_path}` (authoritative ticket specification; treat Parent Spec section as background context only).",
        "2. Read the redacted baseline standards, glossary, and active ADR snapshots listed here:",
        *([f"- `{path}`" for path in standard_paths] or ["- (none present in baseline)"]),
        "",
        "## Execution Rules",
        "- Minimal complete change: satisfy ONLY the ticket's acceptance criteria.",
        "- Record independent bugs or cleanup opportunities in `discovered_issues`; leave unrelated files untouched.",
        gate_line,
        f"- {verify_line}",
        "- Leave `git commit` and `git push` to the orchestrator (leave your tested edits cleanly in the worktree).",
        "",
        "## TDD Reference",
        tdd_rules.strip(),
    ]
    if feedback:
        lines += ["", "## Mandatory Rework Feedback (address every item below)", feedback.strip()]
    lines += ["", "Return ONLY a JSON object matching the required schema."]
    return "\n".join(lines) + "\n"


def _write_review_diff(
    worktree: Path,
    base_sha: str,
    candidate_sha: str,
    diff_patch_path: Path,
    *,
    block_added_secrets: bool,
) -> bool:
    # Standalone review blocks added secrets. Redact deleted secret lines before any review.
    diff_output = run_cmd(
        git_cmd(
            "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color", f"{base_sha}..{candidate_sha}",
            attr_source=base_sha,
        ),
        cwd=worktree,
        check=True,
        env_role="orchestrator",
        cap_output=False,
        stdout_encoding="latin-1",
    ).stdout.encode("latin-1")
    raw_diff_text = diff_output.decode("utf-8", errors="replace")
    if block_added_secrets:
        added_secret_hits = scan_range_for_secrets(worktree, base_sha, candidate_sha)
        if added_secret_hits:
            raise RuntimeError(
                "Refusing to send candidate diff containing added secrets to reviewer:\n"
                + "\n".join(f"- {h}" for h in sorted(set(added_secret_hits)))
            )
    diff_text = redact_secrets(raw_diff_text)
    if "\x00" in raw_diff_text:
        # A diff attribute can force UTF-16 text, including removed credentials, into the review patch.
        # Preserve BOMs as such: UTF-8 replacement characters would hide a header at column one.
        ascii_view = (diff_output.replace(codecs.BOM_UTF16_LE, codecs.BOM_UTF8)
                      .replace(codecs.BOM_UTF16_BE, codecs.BOM_UTF8)
                      .replace(b"\x00", b"").decode("utf-8", errors="replace"))
        safe_view = redact_secrets(ascii_view)
        if safe_view != ascii_view:
            diff_text = safe_view
    diff_redacted_for_review = diff_text != raw_diff_text
    atomic_write_text(diff_patch_path, diff_text)
    return diff_redacted_for_review


def _review_artifact_hashes(
    diff_patch_path: Path,
    auth_brief_path: Path | None,
    auth_brief_sha256: str | None,
    baseline_std_dir: Path,
    baseline_std_hashes: dict[str, str],
) -> dict[Path, str]:
    diff_patch_sha256 = sha256_file(diff_patch_path)
    artifact_hashes = {baseline_std_dir / rel: digest for rel, digest in baseline_std_hashes.items()}
    artifact_hashes[diff_patch_path] = diff_patch_sha256
    if auth_brief_path is not None:
        if auth_brief_sha256 is None:
            raise RuntimeError("Missing digest for the authoritative review brief.")
        artifact_hashes[auth_brief_path] = auth_brief_sha256
    return artifact_hashes


def _assert_review_artifacts_intact(artifact_hashes: dict[Path, str]) -> None:
    for path, digest in artifact_hashes.items():
        assert_sha256_intact(path, digest, path.name)


def _extract_ticket_description_for_touches(brief_text: str) -> str:
    """Slice rendered issue snapshot to Ticket Description so Parent Spec/comments cannot pollute Touches."""
    lines = brief_text.splitlines()
    start = 0
    for idx, line in enumerate(lines):
        if line.strip().startswith("## Ticket Description"):
            start = idx + 1
            break
    out: list[str] = []
    fence = ""
    for line in lines[start:]:
        stripped = line.strip()
        if fence:
            out.append(line)
            if stripped.startswith(fence[0] * len(fence)) and set(stripped) == {fence[0]}:
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            out.append(line)
            continue
        if (
            stripped.startswith("## Parent Spec (")
            or stripped == "## Recent Human Comments"
            or stripped == "<!-- END LINEAR DATA -->"
        ):
            break
        out.append(line)
    return "\n".join(out)


def _format_touches_scope_signal(
    worktree: Path,
    base_sha: str,
    candidate_sha: str,
    auth_brief_path: Path | None,
) -> list[str]:
    if auth_brief_path is None or not auth_brief_path.is_file():
        return []
    raw_names = run_cmd(
        git_cmd(
            "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", "--name-only",
            f"{base_sha}..{candidate_sha}", attr_source=base_sha,
        ),
        cwd=worktree,
        check=True,
        env_role="orchestrator",
        cap_output=False,
    ).stdout
    changed_files = [p for p in raw_names.split("\x00") if p]
    shortstat = run_cmd(
        git_cmd(
            "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--shortstat",
            f"{base_sha}..{candidate_sha}", attr_source=base_sha,
        ),
        cwd=worktree,
        check=True,
        env_role="orchestrator",
    ).stdout.strip()
    brief_text = auth_brief_path.read_text(encoding="utf-8", errors="replace")
    touches = parse_touches(_extract_ticket_description_for_touches(brief_text))
    lines = [
        "## Scope Telemetry (Informational — verify against Ticket Brief)",
        f"- Diff summary: {shortstat or f'{len(changed_files)} files changed'}",
    ]
    if not touches:
        lines.append("- Declared `## Touches`: (none or unscoped)")
        return lines
    outside = [
        path for path in changed_files
        if not touches_overlap({path.replace("\\", "/").lstrip("/").lower()}, touches)
    ]
    lines.append(f"- Declared `## Touches` ({len(touches)}): {', '.join(f'`{t}`' for t in sorted(touches))}")
    if outside:
        shown = outside[:20]
        more = f" (+{len(outside) - len(shown)} more)" if len(outside) > len(shown) else ""
        lines.append(
            f"- Modified files outside declared `## Touches` ({len(outside)}/{len(changed_files)}): "
            + ", ".join(f"`{p}`" for p in shown)
            + more
            + " (informational: verify whether each file is strictly required by this slice or represents scope creep)"
        )
    else:
        lines.append(
            f"- Modified files outside declared `## Touches`: none ({len(changed_files)}/{len(changed_files)} within declared touches)"
        )
    return lines


def _build_review_prompts(
    worktree: Path,
    base_sha: str,
    candidate_sha: str,
    diff_patch_path: Path,
    auth_brief_path: Path | None,
    baseline_std_dir: Path,
    baseline_std_hashes: dict[str, str],
    prior_findings: dict[str, list[str]] | None,
    pr_feedback: str | None,
) -> dict[str, str]:
    commits = run_cmd(
        git_cmd("log", f"{base_sha}..{candidate_sha}", "--oneline"), cwd=worktree, check=True, env_role="orchestrator"
    ).stdout.strip()
    diff_stat = run_cmd(
        git_cmd(
            "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--stat", f"{base_sha}..{candidate_sha}",
            attr_source=base_sha,
        ),
        cwd=worktree,
        check=True,
        env_role="orchestrator",
    ).stdout.strip()

    std_rubric = PRELOADED_REFS["standards"]
    spec_rubric = PRELOADED_REFS["spec"]
    prior_std = (prior_findings or {}).get("standards", [])
    prior_spec = (prior_findings or {}).get("spec", [])

    std_lines = [
        "READ-ONLY CODE REVIEW — AXIS: STANDARDS.",
        "Do NOT create, edit, delete, or commit any files, do NOT run tests that write files, and do NOT spawn subagents.",
        "",
        f"Read the complete pre-generated candidate diff at `{diff_patch_path}` (commit range `{base_sha}..{candidate_sha}`).",
        f"Commits:\n{commits or '(none)'}",
        f"Diff stat:\n{diff_stat or '(empty)'}",
        f"Immutable baseline standard snapshots live under `{baseline_std_dir}`: {', '.join(sorted(baseline_std_hashes)) or '(none)'}",
        "",
        std_rubric.strip(),
    ]
    if prior_std:
        std_lines += ["", "## Prior Round Findings (verify resolution first)", *[f"- {x}" for x in prior_std]]
    std_lines += ["", 'Set `axis` to `"standards"`. Return ONLY valid JSON matching the schema.']

    scope_telemetry = _format_touches_scope_signal(worktree, base_sha, candidate_sha, auth_brief_path)
    spec_lines = [
        "READ-ONLY CODE REVIEW — AXIS: SPEC & INTEGRITY.",
        "Do NOT create, edit, delete, or commit any files, do NOT run tests that write files, and do NOT spawn subagents.",
        "",
        f"Read the complete pre-generated candidate diff at `{diff_patch_path}` (commit range `{base_sha}..{candidate_sha}`).",
        f"Commits:\n{commits or '(none)'}",
        f"Diff stat:\n{diff_stat or '(empty)'}",
        f"Immutable authoritative Ticket Brief path: `{auth_brief_path}` (read ONLY this path, not any file inside `.dispatch/`).",
        *(["", *scope_telemetry] if scope_telemetry else []),
        "",
        spec_rubric.strip(),
    ]
    if pr_feedback:
        spec_lines += ["", "## Open PR Review Feedback (verify addressed)", pr_feedback.strip()]
    if prior_spec:
        spec_lines += ["", "## Prior Round Findings (verify resolution first)", *[f"- {x}" for x in prior_spec]]
    spec_lines += ["", 'Set `axis` to `"spec"`. Return ONLY valid JSON matching the schema.']

    return {"standards": "\n".join(std_lines) + "\n", "spec": "\n".join(spec_lines) + "\n"}


def _execute_review_axes(
    reviewer: HarnessSpec,
    worktree: Path,
    run_dir: Path,
    round_num: int,
    prompts: dict[str, str],
    attachments: dict[str, list[Path]],
    artifact_hashes: dict[Path, str],
    trace_sink: list[dict[str, Any]] | None,
    review_timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    def _run_axis(axis: str, prompt_text: str, axis_attach: list[Path]) -> dict[str, Any]:
        last_err: Exception | None = None
        for axis_try in (1, 2):
            check_shutdown()
            for path in axis_attach:
                assert_sha256_intact(path, artifact_hashes[path], path.name)
            suffix = "" if axis_try == 1 else f"-retry{axis_try}"
            log(f"Running {axis} review via {reviewer.label} (attempt {round_num}{suffix})...")
            try:
                return invoke_harness(
                    reviewer,
                    prompt_text,
                    worktree=worktree,
                    run_dir=run_dir,
                    schema=REVIEW_SCHEMA,
                    schema_file=run_dir / f"review-{axis}-schema.json",
                    prompt_file=run_dir / f"prompt-{axis}-r{round_num}{suffix}.md",
                    out_file=run_dir / f"review-{axis}-r{round_num}{suffix}.json",
                    read_only=True,
                    expected_axis=axis,
                    session_label=f"review-{axis}-r{round_num}{suffix}",
                    trace_sink=trace_sink,
                    attach_files=axis_attach,
                    timeout=review_timeout,
                )
            except (ValueError, subprocess.TimeoutExpired, RuntimeError) as exc:
                last_err = exc
                if axis_try == 1 and not is_shutdown_requested():
                    log(f"{axis} review failed transiently ({type(exc).__name__}: {exc}); retrying once...")
                    retry_delay = float(os.environ.get("AGENT_DISPATCH_REVIEW_RETRY_SLEEP", "2.0"))
                    if retry_delay > 0:
                        interruptible_sleep(retry_delay)
                    continue
                raise
        assert last_err is not None
        raise last_err

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_std = pool.submit(_run_axis, "standards", redact_secrets(prompts["standards"]), attachments["standards"])
        f_spec = (
            pool.submit(_run_axis, "spec", redact_secrets(prompts["spec"]), attachments["spec"])
            if "spec" in attachments else None
        )
        standards_res = f_std.result()
        spec_res = (
            f_spec.result()
            if f_spec
            else {
                "axis": "spec",
                "verdict": "skipped",
                "hard_findings": [],
                "suggestions": ["No Linear issue supplied; Spec axis was NOT executed."],
                "test_integrity_violation": False,
                "summary": "Spec axis skipped (no --issue supplied).",
            }
        )

    return standards_res, spec_res


def _snapshot_review_setup_files(
    worktree: Path,
    setup_files: dict[str, tuple[int, int, int, int]] | None,
) -> dict[str, tuple[int, int, int, int]]:
    pre_review_untracked: dict[str, tuple[int, int, int, int]] = {}
    if setup_files:
        cur_untracked = untracked_files(worktree)
        unexpected = cur_untracked - set(setup_files)
        if unexpected:
            raise RuntimeError(f"Unexpected untracked files present before review: {sorted(unexpected)[:5]}")
        pre_review_untracked = {name: file_identity(worktree / name) for name in cur_untracked}
    return pre_review_untracked


def _verify_review_worktree(
    worktree: Path,
    candidate_sha: str,
    pre_review_untracked: dict[str, tuple[int, int, int, int]],
    *,
    has_setup_files: bool,
) -> None:
    head_after = run_cmd(git_cmd("rev-parse", "HEAD"), cwd=worktree, check=True, env_role="orchestrator").stdout.strip()
    if has_setup_files:
        dirty_after = run_cmd(git_cmd("status", "--porcelain", "--untracked-files=no"), cwd=worktree,
                              check=True, env_role="orchestrator").stdout.strip()
        post_untracked = untracked_files(worktree)
        if post_untracked != set(pre_review_untracked) or any(
            not os.path.lexists(worktree / name) or file_identity(worktree / name) != original
            for name, original in pre_review_untracked.items()
        ):
            dirty_after += " untracked files changed during review"
    else:
        dirty_after = run_cmd(git_cmd("status", "--porcelain"), cwd=worktree, check=True, env_role="orchestrator").stdout.strip()
    if head_after != candidate_sha or dirty_after:
        raise RuntimeError(
            f"Reviewer mutated worktree! expected HEAD={candidate_sha}, got {head_after}; dirty={dirty_after!r}"
        )


def run_two_axis_review(
    reviewer: HarnessSpec,
    worktree: Path,
    run_dir: Path,
    base_sha: str,
    candidate_sha: str,
    auth_brief_path: Path | None,
    auth_brief_sha256: str | None,
    baseline_std_dir: Path,
    baseline_std_hashes: dict[str, str],
    round_num: int,
    *,
    prior_findings: dict[str, list[str]] | None = None,
    pr_feedback: str | None = None,
    trace_sink: list[dict[str, Any]] | None = None,
    review_timeout: int = 900,
    block_added_secrets: bool = False,
    setup_files: dict[str, tuple[int, int, int, int]] | None = None,
) -> dict[str, Any]:
    diff_patch_path = run_dir / f"diff-r{round_num}.patch"
    diff_redacted_for_review = _write_review_diff(
        worktree, base_sha, candidate_sha, diff_patch_path, block_added_secrets=block_added_secrets,
    )
    artifact_hashes = _review_artifact_hashes(
        diff_patch_path, auth_brief_path, auth_brief_sha256, baseline_std_dir, baseline_std_hashes,
    )
    _assert_review_artifacts_intact(artifact_hashes)

    prompts = _build_review_prompts(
        worktree, base_sha, candidate_sha, diff_patch_path, auth_brief_path,
        baseline_std_dir, baseline_std_hashes, prior_findings, pr_feedback,
    )
    attachments = {"standards": [diff_patch_path, *(baseline_std_dir / rel for rel in sorted(baseline_std_hashes))]}
    if auth_brief_path:
        attachments["spec"] = [diff_patch_path, auth_brief_path]

    pre_review_untracked = _snapshot_review_setup_files(worktree, setup_files)
    standards_res, spec_res = _execute_review_axes(
        reviewer, worktree, run_dir, round_num, prompts, attachments, artifact_hashes, trace_sink, review_timeout,
    )
    _verify_review_worktree(worktree, candidate_sha, pre_review_untracked, has_setup_files=bool(setup_files))
    _assert_review_artifacts_intact(artifact_hashes)

    spec_ok = (spec_res["verdict"] == "pass") if auth_brief_path else True
    passed = (
        standards_res["verdict"] == "pass"
        and spec_ok
        and not standards_res["test_integrity_violation"]
        and not spec_res["test_integrity_violation"]
        and not standards_res["hard_findings"]
        and not spec_res["hard_findings"]
    )
    return {
        "passed": passed,
        "mode": "two_axis" if auth_brief_path else "standards_only",
        "diff_redacted_for_review": diff_redacted_for_review,
        "standards": standards_res,
        "spec": spec_res,
    }


def format_review_markdown(review: dict[str, Any]) -> str:
    def _fmt(title: str, data: dict[str, Any]) -> list[str]:
        lines = [f"### {title} (`{str(data.get('verdict', 'unknown')).upper()}`)"]
        lines.append(redact_secrets(str(data.get("summary", ""))))
        lines.append("")
        lines.append("**Hard Findings:**")
        hard = data.get("hard_findings") or []
        lines.extend([f"- [HARD] {redact_secrets(x)}" for x in hard] or ["- None"])
        lines.append("")
        lines.append("**Suggestions:**")
        sugg = data.get("suggestions") or []
        lines.extend([f"- [Note] {redact_secrets(x)}" for x in sugg] or ["- None"])
        return lines

    return "\n".join(_fmt("Standards Axis", review["standards"]) + [""] + _fmt("Spec & Integrity Axis", review["spec"]))
