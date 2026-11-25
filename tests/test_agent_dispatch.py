#!/usr/bin/env python3
"""
Unit, security/false-positive, end-to-end CLI fault-injection, parallel frontier, real-signal,
and opt-in live Linear contract tests for agent-dispatch v5 and install.sh.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

# Prevent SourceFileLoader from leaving .pyc artifacts inside bin/__pycache__
sys.dont_write_bytecode = True

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import FakeLinearState, IsolatedDispatchCase, WORKFLOW_ROOT, _git, _write_exec, start_fake_linear

BIN_PATH = WORKFLOW_ROOT / "bin" / "agent-dispatch"
INSTALL_SH = WORKFLOW_ROOT / "install.sh"

loader = importlib.machinery.SourceFileLoader("_agent_dispatch_legacy", str(BIN_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
ad = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ad
loader.exec_module(ad)

from agent_dispatch import config, process


class LegacyDispatchCase(IsolatedDispatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(config, "MCP_CONFIG_PATH", self.sandbox / "unused-mcp.json"))


class TestCLIEntrypoint(IsolatedDispatchCase):
    def test_cli_starts_directly_and_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "agent-dispatch"
            link.symlink_to(BIN_PATH)
            for executable in (BIN_PATH, link):
                with self.subTest(executable=executable):
                    result = subprocess.run([str(executable), "--help"], cwd=td, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage: agent-dispatch", result.stdout)


class TestLegacyUnitContracts(LegacyDispatchCase):
    def test_review_redacts_deleted_json_authorization_credentials(self) -> None:
        token = "synthetic_pd_tok_987654321"
        settings_json = json.dumps(
            {
                "pagerduty": {"headers": {"Authorization": f"Token token={token}"}},
                "proxy": {"headers": {"Proxy-Authorization": f"ApiKey {token}"}},
            },
            indent=2,
        ) + "\n"

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "commit", "--allow-empty", "-m", "base")

            cfg = repo / "settings.json"
            cfg.write_text(settings_json, encoding="utf-8")
            _git(repo, "add", "settings.json")
            _git(repo, "commit", "-m", "add synthetic json credentials")
            introduced = _git(repo, "rev-parse", "HEAD")

            cfg.write_text('{"pagerduty": {"headers": {"Authorization": "Token token=${PAGERDUTY_TOKEN}"}}}\n', encoding="utf-8")
            _git(repo, "commit", "-am", "replace with placeholder")
            cleaned = _git(repo, "rev-parse", "HEAD")

            run_dir = Path(os.environ["AGENT_DISPATCH_CACHE_ROOT"]) / "review-json-redact"
            with mock.patch.object(ad, "invoke_harness", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    ad.run_two_axis_review(
                        ad.parse_harness_spec("codex"), repo, run_dir, introduced, cleaned,
                        None, None, run_dir, {}, 1, block_added_secrets=True,
                    )
            patch_txt = (run_dir / "diff-r1.patch").read_text(encoding="utf-8")
            self.assertNotIn(token, patch_txt)
            self.assertIn("<REDACTED_SECRET>", patch_txt)

    def test_review_rechecks_diff_before_retrying_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _git(d, "init", "-b", "main")
            _git(d, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "test")
            head = _git(d, "rev-parse", "HEAD")
            run_dir = Path(os.environ["AGENT_DISPATCH_CACHE_ROOT"]) / "review"
            def change_diff_and_fail(*args, **kwargs):
                (run_dir / "diff-r1.patch").write_text("modified after first review")
                raise ValueError("incomplete verdict")
            with mock.patch.object(ad, "invoke_harness", side_effect=change_diff_and_fail) as harness:
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REVIEW_RETRY_SLEEP": "0"}):
                    with self.assertRaisesRegex(RuntimeError, "integrity violation.*diff-r1"):
                        ad.run_two_axis_review(ad.parse_harness_spec("agy"), d, run_dir, head, head,
                                               None, None, run_dir / "standards", {}, 1)
                self.assertEqual(harness.call_count, 1)

    def test_worker_rechecks_immutable_brief_before_timeout_rework(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "base")
            linear = FakeLinearState("test")
            linear.add_issue("TST-1", "Implement task", "## What to Build\nFeature")
            srv, url = start_fake_linear(linear)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            def change_brief_and_timeout(spec, prompt, **kwargs):
                brief = kwargs["run_dir"] / "brief.md"
                self.assertIn(str(brief), prompt)
                brief.write_text("changed during first worker")
                raise subprocess.TimeoutExpired("fake worker", 1)
            with mock.patch.object(ad, "invoke_harness", side_effect=change_brief_and_timeout) as harness:
                with self.assertRaisesRegex(RuntimeError, "integrity violation.*brief.md"):
                    ad.execute_ticket(ad.LinearClient("synthetic", url=url), repo, None, "TST-1",
                                      ad.parse_harness_spec("codex"), ad.parse_harness_spec("agy"),
                                      verify_cmd_override="true", setup_cmd_override=None, max_rework=1,
                                      verify_timeout=5, setup_timeout=5, no_push=True, allow_no_verify=False,
                                      allow_gate_edits=False, allow_test_edits=False, allow_dirty_docs=True,
                                      skip_baseline=True, skip_project_check=True, force_rerun=False, fresh=False)
                self.assertEqual(harness.call_count, 1)

    def test_pr_markdown_rendering(self) -> None:
        body = ad.build_pr_body(
            {"identifier": "K1N-99", "title": "Add auth", "url": "https://linear.app/k1n/issue/K1N-99"},
            "a" * 40,
            ad.parse_harness_spec("codex:sol"),
            ad.parse_harness_spec("agy:neon"),
            "Implemented auth with lin_api_SECRET1234567890",
            ["pytest"],
            {"ran": True, "command": "pytest", "passed": True},
            {
                "passed": True,
                "standards": {"verdict": "pass", "summary": "OK", "hard_findings": [], "suggestions": ["Rename x"]},
                "spec": {"verdict": "pass", "summary": "All criteria met", "hard_findings": [], "suggestions": []},
            },
            ["[attempt 1] Side bug"],
        )
        for line in body.splitlines():
            self.assertFalse(line.startswith("    "), f"Broken 4-space codeblock indent: {line!r}")
        self.assertNotIn("lin_api_SECRET", body)
        self.assertIn("<REDACTED_SECRET>", body)

    def test_review_redacts_deleted_utf16_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "commit", "--allow-empty", "-m", "base")
            file = repo / "config.txt"
            (repo / ".gitattributes").write_text("config.txt diff\n")
            _git(repo, "add", ".gitattributes")
            _git(repo, "commit", "-m", "show text diff")
            for encoding in ("utf-16-le", "utf-16-be"):
                with self.subTest(encoding=encoding):
                    original = "\ufeff项目\r\nsetting="
                    token = "lin_api_" + "SYNTHETIC" * 3
                    file.write_bytes((original + token + "\r\n结尾").encode(encoding))
                    _git(repo, "add", "config.txt")
                    _git(repo, "commit", "-m", "introduce synthetic fixture")
                    introduced = _git(repo, "rev-parse", "HEAD")
                    file.write_bytes((original + "empty\r\n结尾").encode(encoding))
                    _git(repo, "commit", "-am", "remove synthetic fixture")
                    run_dir = Path(os.environ["AGENT_DISPATCH_CACHE_ROOT"]) / encoding
                    with mock.patch.object(ad, "invoke_harness", side_effect=SystemExit):
                        with self.assertRaises(SystemExit):
                            ad.run_two_axis_review(ad.parse_harness_spec("codex"), repo, run_dir, introduced,
                                                   _git(repo, "rev-parse", "HEAD"), None, None, run_dir, {}, 1)
                    patch = (run_dir / "diff-r1.patch").read_text()
                    self.assertNotIn(token, patch.replace("\0", ""))
                    self.assertIn("<REDACTED_SECRET>", patch)
                    _git(repo, "rm", "config.txt")
                    _git(repo, "commit", "-m", "remove file")

    def test_review_redacts_context_and_deleted_credential_header_after_utf16_bom(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / ".gitattributes").write_text("request.txt diff\n")
            path = repo / "request.txt"
            token = "synthetic.opaque.value"
            for encoding in ("utf-16-le", "utf-16-be"):
                with self.subTest(encoding=encoding):
                    header = "\ufeffAuthorization: token " + token + "\r\n"
                    path.write_bytes((header + "old\r\n").encode(encoding))
                    _git(repo, "add", ".")
                    _git(repo, "commit", "-m", "synthetic fixture")
                    for content in (header + "changed\r\n", "\ufeffremoved\r\n"):
                        path.write_bytes(content.encode(encoding))
                        _git(repo, "commit", "-am", "edit fixture")
                        run_dir = Path(os.environ["AGENT_DISPATCH_CACHE_ROOT"]) / encoding
                        with mock.patch.object(ad, "invoke_harness", side_effect=SystemExit):
                            with self.assertRaises(SystemExit):
                                ad.run_two_axis_review(ad.parse_harness_spec("codex"), repo, run_dir, "HEAD^",
                                                       _git(repo, "rev-parse", "HEAD"), None, None, run_dir, {}, 1,
                                                       block_added_secrets=True)
                        patch = (run_dir / "diff-r1.patch").read_text()
                        self.assertNotIn(token, patch.replace("\0", ""))
                        self.assertIn("<REDACTED_SECRET>", patch)

    def test_stash_rejects_secret_edits_without_writing_git_objects(self) -> None:
        token = "lin_api_" + "SYNTHETICWORKTREEEXAMPLE"
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Test")
            tracked = repo / "tracked.txt"
            tracked.write_text("base")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "base")
            for path in (tracked, repo / "new.dat"):
                path.write_text(token)
                digest = _git(repo, "hash-object", str(path))
                index_before = _git(repo, "ls-files", "--stage")
                ad._stash_dirty_worktree_for_safe_rerun(repo, "halt")
                self.assertEqual(_git(repo, "stash", "list"), "")
                self.assertEqual(_git(repo, "ls-files", "--stage"), index_before)
                self.assertEqual(path.read_text(), token)
                self.assertEqual(_git(repo, "cat-file", "-t", digest, check=False), "")
                if path == tracked:
                    path.write_text("base")
                else:
                    path.unlink()
            # The stash index parent would contain this value even when the worktree is clean.
            tracked.write_text(token)
            _git(repo, "add", str(tracked))
            tracked.write_text("base")
            index_before = _git(repo, "ls-files", "--stage")
            ad._stash_dirty_worktree_for_safe_rerun(repo, "halt")
            self.assertEqual(_git(repo, "stash", "list"), "")
            self.assertEqual(_git(repo, "ls-files", "--stage"), index_before)
            self.assertEqual(tracked.read_text(), "base")
            _git(repo, "reset", "--", "tracked.txt")
            (repo / "safe.txt").write_text("save me")
            process.request_shutdown(signal.SIGHUP)
            try:
                ad._stash_dirty_worktree_for_safe_rerun(repo, "halt")
            finally:
                process.reset_shutdown()
            self.assertIn("halt", _git(repo, "stash", "list"))
            self.assertEqual(_git(repo, "show", "stash^3:safe.txt"), "save me")

    def test_touches_wave_selection_defers_overlapping_and_unscoped_issues(self) -> None:
        # Hierarchical Touches overlap: src/routes and src/routes/admin MUST NOT share a wave!
        iss_a = {"identifier": "TST-1", "description": "## Touches\n- src/routes\n"}
        iss_b = {"identifier": "TST-2", "description": "## Touches\n- src/auth\n"}
        iss_c = {"identifier": "TST-3", "description": "## Touches\n- src/routes/admin\n"}
        iss_d = {"identifier": "TST-4", "description": "## What to Build\nno touches declared"}
        wave, deferred = ad.select_non_overlapping_wave([iss_a, iss_b, iss_c, iss_d])
        self.assertEqual([i["identifier"] for i in wave], ["TST-1", "TST-2"])
        self.assertEqual([d["id"] for d in deferred], ["TST-3", "TST-4"])

    def test_pr_feedback_trust_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 1. PR feedback: empty authorAssociation & NONE MUST be rejected; OWNER accepted; paginated --slurp flattened!
            def fake_gh(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if cmd[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{"number": 7, "url": "u", "isDraft": True, "isCrossRepository": False, "headRefName": "feat/x"}, {"number": 8, "isCrossRepository": True, "headRefName": "feat/x"}]), stderr="")
                if cmd[:3] == ["gh", "pr", "view"]:
                    payload = {
                        "reviews": [
                            {"authorAssociation": "", "state": "CHANGES_REQUESTED", "body": "untrusted empty", "author": {"login": "anon"}},
                            {"authorAssociation": "NONE", "state": "CHANGES_REQUESTED", "body": "untrusted none", "author": {"login": "stranger"}},
                            {"authorAssociation": "OWNER", "state": "CHANGES_REQUESTED", "body": "fix null check", "author": {"login": "k1n"}},
                        ],
                        "comments": [
                            {"authorAssociation": "", "body": "ignore me", "author": {"login": "x"}},
                        ],
                    }
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
                if cmd[:2] == ["gh", "api"]:
                    pages = [
                        [{"author_association": "", "body": "bad inline", "path": "a.py", "line": 1, "user": {"login": "u"}}],
                        [{"author_association": "MEMBER", "body": "good inline", "path": "b.py", "line": 9, "user": {"login": "m"}}],
                    ]
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pages), stderr="")
                raise AssertionError(f"Unexpected cmd: {cmd}")

            with mock.patch.object(ad, "run_cmd", side_effect=fake_gh):
                issue_pr = ad.fetch_open_pr_for_branch(d, "test/repo", "feat/x")
                self.assertEqual(issue_pr["number"], 7)
                fb = ad.fetch_open_pr_feedback(d, "test/repo", issue_pr["number"]) or ""
            self.assertIn("fix null check", fb)
            self.assertIn("good inline", fb)
            self.assertNotIn("untrusted empty", fb)
            self.assertNotIn("untrusted none", fb)
            self.assertNotIn("bad inline", fb)

    def test_reconcile_publish_prepush_non_draft_guard_and_missing_pr_readback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            bare = d / "origin.git"
            repo = d / "repo"
            _git(d, "init", "--bare", "-b", "main", str(bare))
            _git(d, "clone", str(bare), str(repo))
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "a.txt").write_text("v1\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            _git(repo, "push", "-u", "origin", "main")
            _git(repo, "checkout", "-b", "feat/tst-p")
            (repo / "a.txt").write_text("v2\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "feat")
            cand_sha = _git(repo, "rev-parse", "HEAD")

            ls_state = FakeLinearState(project_name="repo")
            iss = ls_state.add_issue("TST-P", "Clean title", "desc", state_name="In Progress", labels=["agent-ready"])
            srv, url = start_fake_linear(ls_state)
            try:
                client = ad.LinearClient("lin_api_fake_1234567890", url=url)
                run_dir = d / "run"
                run_dir.mkdir()
                latest_f = d / "latest.json"
                state = {
                    "run_id": "r1",
                    "publish_destination": ad.sha256_text(str(bare)),
                    "branch": "feat/tst-p",
                    "base_branch": "main",
                    "base_sha": _git(repo, "rev-parse", "main"),
                    "candidate_sha": cand_sha,
                    "pr_body": "body",
                    "worker": "codex:sol",
                    "reviewer": "agy:neon",
                    "attempt": 1,
                    "max_attempts": 3,
                    "worker_summary": "ok",
                    "review": {"standards": {"verdict": "pass", "summary": "ok"}, "spec": {"verdict": "pass", "summary": "ok"}},
                    "stage": "verified_and_reviewed",
                }
                # 1. Existing non-Draft PR MUST fail BEFORE git push!
                non_draft = {"state": "OPEN", "number": 5, "url": "u", "isDraft": False, "baseRefName": "main", "headRefOid": "0" * 40}
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_FORCE_GH": "1", "AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                    with mock.patch.object(ad, "fetch_open_pr_for_branch", return_value=non_draft):
                        with self.assertRaisesRegex(RuntimeError, "not a Draft PR"):
                            ad.reconcile_publish(client, iss, repo, run_dir, latest_f, dict(state))
                # A legacy GitHub checkpoint still has the identity of the original owner/repo.
                _git(repo, "remote", "set-url", "origin", "https://github.com/test/repo.git")
                legacy = dict(state, repo_identity=ad.canonical_repo_identity(repo))
                del legacy["publish_destination"]
                with mock.patch.object(ad, "fetch_open_pr_for_branch", return_value=non_draft):
                    with self.assertRaisesRegex(RuntimeError, "not a Draft PR"):
                        ad.reconcile_publish(client, iss, repo, run_dir, latest_f, legacy)
                _git(repo, "remote", "set-url", "origin", str(bare))
                # Remote branch MUST NOT have been pushed!
                self.assertEqual(ad.get_remote_branch_sha(repo, "feat/tst-p"), "")

                # 2. Missing read-back after gh pr create MUST stay at stage='pushed' (NEVER 'completed')!
                st2 = dict(state)
                with mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_DISPATCH_FORCE_GH": "1",
                        "AGENT_DISPATCH_CACHE_ROOT": str(d / "cache"),
                        "AGENT_DISPATCH_PR_POLL_ATTEMPTS": "1",
                        "AGENT_DISPATCH_PR_POLL_SLEEP": "0",
                    },
                ):
                    with mock.patch.object(ad, "fetch_open_pr_for_branch", return_value=None):
                        orig_run = ad.run_cmd
                        def intercept_gh(cmd: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
                            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "create"]:
                                return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/t/r/pull/9\n", stderr="")
                            return orig_run(cmd, **kw)
                        with mock.patch.object(ad, "run_cmd", side_effect=intercept_gh):
                            with self.assertRaisesRegex(RuntimeError, "no open PR"):
                                ad.reconcile_publish(client, iss, repo, run_dir, latest_f, st2)
                self.assertEqual(st2["stage"], "pushed")
                self.assertTrue(st2["pushed"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_publish_signal_preserves_resumable_stage_and_review_retries_json_and_checks_standards_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            bare = d / "origin.git"
            repo = d / "repo"
            _git(d, "init", "--bare", "-b", "main", str(bare))
            _git(d, "clone", str(bare), str(repo))
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "CODING_STANDARDS.md").write_text("# Standards\n", encoding="utf-8")
            (repo / "a.py").write_text("def x(): return 1\n", encoding="utf-8")
            (repo / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_x(self): self.assertEqual(1,1)\n", encoding="utf-8")
            (repo / "Makefile").write_text("verify:\n\tpython3 -m unittest test_a.py\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            _git(repo, "push", "-u", "origin", "main")
            base_sha = _git(repo, "rev-parse", "HEAD")

            # 1. Review JSON parse retry (first attempt returns garbage, second returns valid JSON -> succeeds!)
            #    AND verify post-review tampering of baseline-standards/CODING_STANDARDS.md raises integrity violation!
            _git(repo, "checkout", "-b", "feat/r")
            (repo / "a.py").write_text("def x(): return 2\n", encoding="utf-8")
            _git(repo, "add", "a.py")
            _git(repo, "commit", "-m", "change")
            cand_sha = _git(repo, "rev-parse", "HEAD")

            run_dir = d / "rev-run"
            std_dir = run_dir / "baseline-standards"
            _, std_hashes = ad.snapshot_baseline_standards(repo, base_sha, std_dir)

            attempts_seen = {"n": 0}
            def flaky_review_invoke(spec: Any, prompt: str, **kw: Any) -> dict[str, Any]:
                attempts_seen["n"] += 1
                if attempts_seen["n"] == 1:
                    raise ValueError("malformed JSON on first try")
                return {
                    "axis": kw["expected_axis"],
                    "verdict": "pass",
                    "hard_findings": [],
                    "suggestions": [],
                    "test_integrity_violation": False,
                    "summary": "ok",
                }
            with mock.patch.object(ad, "invoke_harness", side_effect=flaky_review_invoke):
                res_rev = ad.run_two_axis_review(
                    ad.parse_harness_spec("agy:neon"),
                    repo,
                    run_dir,
                    base_sha,
                    cand_sha,
                    None,
                    None,
                    std_dir,
                    std_hashes,
                    1,
                )
            self.assertTrue(res_rev["passed"])
            self.assertEqual(attempts_seen["n"], 2)

            # Now simulate reviewer tampering with baseline-standards/CODING_STANDARDS.md -> MUST raise integrity error!
            def tamper_std_invoke(spec: Any, prompt: str, **kw: Any) -> dict[str, Any]:
                (std_dir / "CODING_STANDARDS.md").write_text("# TAMPERED\n", encoding="utf-8")
                return {
                    "axis": kw["expected_axis"],
                    "verdict": "pass",
                    "hard_findings": [],
                    "suggestions": [],
                    "test_integrity_violation": False,
                    "summary": "ok",
                }
            with mock.patch.object(ad, "invoke_harness", side_effect=tamper_std_invoke):
                with self.assertRaisesRegex(RuntimeError, "integrity violation.*CODING_STANDARDS"):
                    ad.run_two_axis_review(
                        ad.parse_harness_spec("agy:neon"),
                        repo,
                        run_dir,
                        base_sha,
                        cand_sha,
                        None,
                        None,
                        std_dir,
                        std_hashes,
                        2,
                    )

            # 3. Publish-phase KeyboardInterrupt AFTER push_attempted=True MUST keep stage='pushing' (NOT 'error') and NOT roll back Linear to Todo!
            _git(repo, "checkout", "main")
            ls_state = FakeLinearState(project_name="repo")
            iss = ls_state.add_issue("TST-SIG", "Signal during publish", "## What to Build\nx\n", labels=["agent-ready"])
            srv, url = start_fake_linear(ls_state)
            try:
                client = ad.LinearClient("lin_api_fake_1234567890", url=url)
                cache_dir = d / "sig-cache"

                def fake_worker_invoke(spec: Any, prompt: str, **kw: Any) -> dict[str, Any]:
                    if not kw["read_only"]:
                        (kw["worktree"] / "a.py").write_text("def x(): return 99\n", encoding="utf-8")
                        return {"status": "completed", "summary": "done", "tests_run": ["unittest"], "discovered_issues": [], "blockers": ""}
                    return {"axis": kw["expected_axis"], "verdict": "pass", "hard_findings": [], "suggestions": [], "test_integrity_violation": False, "summary": "ok"}

                def interrupt_during_publish(c: Any, i: Any, wt: Path, rd: Path, lf: Path, st: dict[str, Any], **kw: Any) -> None:
                    st["push_attempted"] = True
                    st["stage"] = "pushing"
                    ad.save_checkpoint(rd, lf, st)
                    process.request_shutdown(signal.SIGINT)
                    raise KeyboardInterrupt("Simulated SIGINT mid-push")

                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(cache_dir)}):
                    with mock.patch.object(ad, "invoke_harness", side_effect=fake_worker_invoke):
                        with mock.patch.object(ad, "reconcile_publish", side_effect=interrupt_during_publish):
                            try:
                                with self.assertRaises(KeyboardInterrupt):
                                    ad.execute_ticket(
                                        client,
                                        repo,
                                        None,
                                        "TST-SIG",
                                        ad.parse_harness_spec("codex:sol"),
                                        ad.parse_harness_spec("agy:neon"),
                                        verify_cmd_override=None,
                                        setup_cmd_override=None,
                                        max_rework=0,
                                        verify_timeout=60,
                                        setup_timeout=60,
                                        no_push=False,
                                        allow_no_verify=False,
                                        allow_gate_edits=False,
                                        allow_test_edits=False,
                                        allow_dirty_docs=True,
                                        skip_baseline=True,
                                        skip_project_check=True,
                                        force_rerun=False,
                                        fresh=False,
                                    )
                            finally:
                                process.reset_shutdown()
                rkey = ad.repo_cache_key(repo)
                ckpt = json.loads((cache_dir / "runs" / rkey / "TST-SIG" / "latest.json").read_text(encoding="utf-8"))
                self.assertEqual(ckpt["stage"], "pushing", "Publish-phase signal MUST NOT overwrite stage to 'error'!")
                self.assertTrue(ckpt["push_attempted"])
                # Even with shutdown requested, finally MUST write dispatch:publish-incomplete + comment!
                self.assertEqual(iss["state"]["name"], "In Progress")
                self.assertIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
                self.assertNotIn("dispatch:failed", [l["name"] for l in iss["_labels"]])
                self.assertTrue(any("Publish Interrupted" in c["body"] for c in ls_state.comments[iss["id"]]))
            finally:
                srv.shutdown()
                srv.server_close()

    def test_review_blocks_added_secrets_redacts_removed_secrets_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Standalone review blocks added secret BEFORE calling reviewer harness (0 LLM calls!)
            s_repo = d / "sec_rev"
            s_repo.mkdir()
            _git(s_repo, "init", "-b", "main")
            _git(s_repo, "config", "user.email", "t@example.com")
            _git(s_repo, "config", "user.name", "T")
            (s_repo / "a.py").write_text("OLD = 'lin_api_OLDSECRET1234567890'\n", encoding="utf-8")
            _git(s_repo, "add", "-A")
            _git(s_repo, "commit", "-m", "init")
            b_sha = _git(s_repo, "rev-parse", "HEAD")
            (s_repo / "a.py").write_text("NEW = 'github_pat_11ABCDEFG01234567890_abcdefghijklmnop'\n", encoding="utf-8")
            _git(s_repo, "add", "-A")
            _git(s_repo, "commit", "-m", "add secret")
            c_sha = _git(s_repo, "rev-parse", "HEAD")
            rev_calls = {"n": 0}
            def count_invoke(*a: Any, **kw: Any) -> dict[str, Any]:
                rev_calls["n"] += 1
                return {"axis": kw["expected_axis"], "verdict": "pass", "hard_findings": [], "suggestions": [], "test_integrity_violation": False, "summary": "ok"}
            with mock.patch.object(ad, "invoke_harness", side_effect=count_invoke):
                with self.assertRaisesRegex(RuntimeError, "added secrets"):
                    ad.run_two_axis_review(
                        ad.parse_harness_spec("agy:neon"), s_repo, d / "r_sec", b_sha, c_sha, None, None, d / "std", {}, 1, block_added_secrets=True
                    )
            self.assertEqual(rev_calls["n"], 0, "Reviewer harness MUST NOT be invoked when added secrets detected!")

            # Removing old secret redacts deleted secret line from diff-r1.patch before reviewer sees it!
            (s_repo / "a.py").write_text("CLEAN = 'ok'\n", encoding="utf-8")
            _git(s_repo, "add", "-A")
            _git(s_repo, "commit", "-m", "remove secret")
            clean_sha = _git(s_repo, "rev-parse", "HEAD")
            with mock.patch.object(ad, "invoke_harness", side_effect=count_invoke):
                res_cln = ad.run_two_axis_review(
                    ad.parse_harness_spec("agy:neon"), s_repo, d / "r_cln", c_sha, clean_sha, None, None, d / "std2", {}, 1, block_added_secrets=True
                )
            self.assertTrue(res_cln["diff_redacted_for_review"])
            self.assertNotIn("github_pat_11ABC", (d / "r_cln" / "diff-r1.patch").read_text(encoding="utf-8"))

            # Reviewer transient RuntimeError auto-retries once!
            flaky_n = {"n": 0}
            def flaky_rt(*a: Any, **kw: Any) -> dict[str, Any]:
                flaky_n["n"] += 1
                if flaky_n["n"] == 1:
                    raise RuntimeError("transient 503")
                return {"axis": kw["expected_axis"], "verdict": "pass", "hard_findings": [], "suggestions": [], "test_integrity_violation": False, "summary": "ok"}
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REVIEW_RETRY_SLEEP": "0"}):
                with mock.patch.object(ad, "invoke_harness", side_effect=flaky_rt):
                    r_ok = ad.run_two_axis_review(
                        ad.parse_harness_spec("agy:neon"), s_repo, d / "r_flk", b_sha, clean_sha, None, None, d / "std3", {}, 1
                    )
            self.assertTrue(r_ok["passed"])
            self.assertEqual(flaky_n["n"], 2)

    def test_fetch_open_pr_rejects_multiple_open_prs_for_same_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # Multi-PR on same head fails closed BEFORE git push!
            two_prs = [
                {"number": 1, "url": "u1", "state": "OPEN", "isDraft": True, "baseRefName": "main", "headRefOid": "a" * 40},
                {"number": 2, "url": "u2", "state": "OPEN", "isDraft": False, "baseRefName": "release", "headRefOid": "a" * 40},
            ]
            with mock.patch.object(ad, "fetch_open_prs_for_branch", return_value=two_prs):
                with self.assertRaisesRegex(RuntimeError, "Multiple open PRs share head branch"):
                    ad.fetch_open_pr_for_branch(repo, "org/repo", "feat/x")

    def test_review_tolerates_deleted_setup_files_and_detects_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "init")
            base = _git(repo, "rev-parse", "HEAD")

            (repo / "venv_cacert.pem").write_text("bundle\n", encoding="utf-8")
            (repo / "cache.tmp").write_text("v1\n", encoding="utf-8")
            setup_files = {
                "venv_cacert.pem": ad._file_identity(repo / "venv_cacert.pem"),
                "cache.tmp": ad._file_identity(repo / "cache.tmp"),
            }
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            # Simulate verify/worker deleting cache.tmp before review while venv_cacert.pem remains
            (repo / "cache.tmp").unlink()
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "candidate")
            cand = _git(repo, "rev-parse", "HEAD")

            def clean_reviewer(*args: Any, **kwargs: Any) -> dict[str, Any]:
                return {
                    "axis": kwargs["expected_axis"],
                    "verdict": "pass",
                    "hard_findings": [],
                    "suggestions": [],
                    "test_integrity_violation": False,
                    "summary": "ok",
                }
            with mock.patch.object(ad, "invoke_harness", side_effect=clean_reviewer):
                rev = ad.run_two_axis_review(
                    ad.parse_harness_spec("agy:neon"), repo, root / "r1", base, cand,
                    None, None, root / "std", {}, 1, setup_files=setup_files,
                )
            self.assertTrue(rev["passed"])

            def mutating_reviewer(*args: Any, **kwargs: Any) -> dict[str, Any]:
                (repo / "venv_cacert.pem").write_text("tampered\n", encoding="utf-8")
                return clean_reviewer(*args, **kwargs)
            with mock.patch.object(ad, "invoke_harness", side_effect=mutating_reviewer):
                with self.assertRaisesRegex(RuntimeError, "Reviewer mutated worktree"):
                    ad.run_two_axis_review(
                        ad.parse_harness_spec("agy:neon"), repo, root / "r2", base, cand,
                        None, None, root / "std", {}, 1, setup_files=setup_files,
                    )

    def test_wildcard_touches_reserve_all_matching_descendants(self) -> None:
        candidates = [{"identifier": "A", "description": "## Touches\n- src/*.py"},
                      {"identifier": "B", "description": "## Touches\n- src/core.py"}]
        selected, deferred = ad.select_non_overlapping_wave(candidates)
        self.assertEqual(selected, candidates[:1])
        self.assertEqual([entry["id"] for entry in deferred], ["B"])

    def test_numbered_touches_conflict_in_wave_and_locks(self) -> None:
        # Unordered list and numbered list for the same path MUST conflict in wave selection and local touch locks.
        iss_bullet = {"identifier": "TST-BUL", "description": "## Touches\n- src/auth\n"}
        iss_num = {"identifier": "TST-NUM", "description": "## Touches\n1. `src/auth`\n"}
        iss_paren = {"identifier": "TST-PAR", "description": "## Touches\n2) `src/auth/login`\n"}
        iss_other = {"identifier": "TST-OTH", "description": "## Touches\n10. `src/billing`\n"}
        wave, deferred = ad.select_non_overlapping_wave([iss_bullet, iss_num, iss_paren, iss_other])
        self.assertEqual([i["identifier"] for i in wave], ["TST-BUL", "TST-OTH"])
        self.assertEqual([d["id"] for d in deferred], ["TST-NUM", "TST-PAR"])

        t_bullet = ad.parse_touches(iss_bullet["description"])
        t_num = ad.parse_touches(iss_num["description"])
        with ad.reserve_touches("repo-touches-num-test", t_bullet) as locked1:
            self.assertTrue(locked1)
            with ad.reserve_touches("repo-touches-num-test", t_num) as locked2:
                self.assertFalse(locked2)

# ---------------------------------------------------------------------------
# End-to-End Subprocess CLI Fault-Injection Suite
# ---------------------------------------------------------------------------

class TestEndToEndCLIFaultInjection(LegacyDispatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.cache_dir = self.root / "cache"
        self.fake_bin = self.root / "fakebin"
        self.fake_bin.mkdir()
        self.bare = self.root / "demo-repo.git"
        self.repo = self.root / "demo-repo"
        self.gh_state_file = self.root / "gh_prs.json"
        self.gh_state_file.write_text("[]", encoding="utf-8")

        _git(self.root, "init", "--bare", "-b", "main", str(self.bare))
        _git(self.root, "clone", str(self.bare), str(self.repo))
        _git(self.repo, "config", "user.email", "dev@example.com")
        _git(self.repo, "config", "user.name", "Dev")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        # Real unittest.TestCase so `python3 -m unittest test_app.py` genuinely executes tests (> 0 tests)!
        (self.repo / "test_app.py").write_text(
            "import unittest\nfrom app import add\nclass TestApp(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n",
            encoding="utf-8",
        )
        (self.repo / "Makefile").write_text("verify:\n\tpython3 -m unittest test_app.py\n", encoding="utf-8")
        (self.repo / "CODING_STANDARDS.md").write_text("# Standards\nKeep functions simple.\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "initial")
        _git(self.repo, "push", "-u", "origin", "main")

        self.linear_state = FakeLinearState(project_name="demo-repo")
        self.linear_srv, self.linear_url = start_fake_linear(self.linear_state)

        self._install_fake_shims()

    def tearDown(self) -> None:
        self.linear_srv.shutdown()
        self.linear_srv.server_close()
        self.td.cleanup()

    def _install_fake_shims(self) -> None:
        # Fake codex shim: asserts child isolation (NO LINEAR_API_KEY, AGENT_DISPATCH_CHILD=1, NO --trust)
        _write_exec(
            self.fake_bin / "codex",
            f"""#!/usr/bin/env python3
import fcntl, json, os, sys
from pathlib import Path

assert os.environ.get("AGENT_DISPATCH_CHILD") == "1", "Missing anti-nesting env"
assert "LINEAR_API_KEY" not in os.environ, "LINEAR_API_KEY leaked to codex!"
assert "GH_TOKEN" not in os.environ, "GH_TOKEN leaked to codex!"
assert "--trust" not in sys.argv, "--trust must not be passed (mutates global config.toml)!"
assert "mcp_servers.linear.enabled=false" in sys.argv, "Linear MCP must be disabled in headless codex!"

# Emit the actual Codex JSONL protocol and validate the working directory
args = sys.argv[1:]
assert "--json" in args and "-o" not in args
cwd = Path(args[args.index("-C") + 1])
mode = os.environ.get("FAKE_CODEX_MODE", "good")
counter_file = Path("{self.root}/codex_calls.count")
with counter_file.open("a+") as counter:
    fcntl.flock(counter, fcntl.LOCK_EX)
    counter.seek(0)
    calls = int(counter.read() or "0") + 1
    counter.seek(0)
    counter.truncate()
    counter.write(str(calls))

print(json.dumps({{"type": "thread.started", "thread_id": f"11111111-2222-3333-4444-{{calls:012d}}"}}), flush=True)
print(json.dumps({{"type": "turn.started"}}))
def finish(res):
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": json.dumps(res)}}}}))
    print(json.dumps({{"type": "turn.completed"}}))

if mode == "tamper_gate_then_fix" and calls == 1:
    (cwd / "Makefile").write_text("verify:\\n\\ttrue\\n", encoding="utf-8")
    (cwd / "app.py").write_text("def add(a, b):\\n    return a + b\\ndef mul(a, b):\\n    return a * b\\n", encoding="utf-8")
elif mode == "tamper_gate_then_fix" and calls >= 2:
    # Restore Makefile from base commit (origin/main) and write proper test
    import subprocess
    subprocess.run(["git", "checkout", "origin/main", "--", "Makefile"], cwd=str(cwd), check=True)
    (cwd / "test_app.py").write_text(
        "import unittest\\nfrom app import add, mul\\nclass T(unittest.TestCase):\\n"
        "    def test_add(self): self.assertEqual(add(1,2), 3)\\n"
        "    def test_mul(self): self.assertEqual(mul(2,3), 6)\\n",
        encoding="utf-8",
    )
elif mode == "blocked_dirty":
    (cwd / "uncommitted_scratch.txt").write_text("partial work\\n", encoding="utf-8")
    res = {{
        "status": "blocked",
        "summary": "Need clarification",
        "tests_run": [],
        "discovered_issues": [],
        "blockers": "Missing API contract",
    }}
    finish(res)
    sys.exit(0)
elif mode == "add_backend_checks":
    (cwd / "tests").mkdir(exist_ok=True)
    check = "self.fail('first backend candidate')" if calls == 1 else "self.assertEqual(2 + 2, 4)"
    (cwd / "tests/test_backend.py").write_text(
        "import unittest\\nclass Backend(unittest.TestCase):\\n    def test_sum(self): " + check + "\\n"
    )
    if calls > 1:
        (cwd / "go.mod").write_text("module example.invalid/project\\n")
elif mode == "parallel_safe":
    import time as _t
    if cwd.name.endswith("P2"):
        _t.sleep(0.5)
    (cwd / f"mod_{{cwd.name.lower()}}.py").write_text(f"VAL = '{{cwd.name}}'\\n", encoding="utf-8")
elif mode == "crash":
    print("simulated codex crash", file=sys.stderr)
    sys.exit(1)
elif mode == "crash_with_linear_outage":
    outage_flag = os.environ.get("FAKE_OUTAGE_FLAG")
    if outage_flag:
        Path(outage_flag).write_text("offline", encoding="utf-8")
    print("simulated network outage during worker", file=sys.stderr)
    sys.exit(1)
elif mode == "edit_gitattributes":
    (cwd / ".gitattributes").write_text("*.py -diff\\n* text=auto\\n", encoding="utf-8")
    (cwd / "app.py").write_text("def add(a, b):\\n    return a + b\\ndef mul(a, b):\\n    return a * b\\n", encoding="utf-8")
    (cwd / "test_app.py").write_text(
        "import unittest\\nfrom app import add, mul\\nclass T(unittest.TestCase):\\n"
        "    def test_add(self): self.assertEqual(add(1,2), 3)\\n"
        "    def test_mul(self): self.assertEqual(mul(2,3), 6)\\n",
        encoding="utf-8",
    )
elif mode == "edit_gitattributes_with_secret":
    (cwd / ".gitattributes").write_text("secret.json -diff\\n", encoding="utf-8")
    (cwd / "secret.json").write_text(json.dumps({{"Authorization": "Token token=synthetic_hidden_secret_12345"}}) + "\\n", encoding="utf-8")
elif mode == "tamper_git_config":
    import subprocess
    subprocess.run(["git", "config", "core.hooksPath", ".husky/_"], cwd=str(cwd), check=True)
    (cwd / "app.py").write_text("def add(a, b):\\n    return a + b\\ndef mul(a, b):\\n    return a * b\\n", encoding="utf-8")
else:
    (cwd / "app.py").write_text("def add(a, b):\\n    return a + b\\ndef mul(a, b):\\n    return a * b\\n", encoding="utf-8")
    (cwd / "test_app.py").write_text(
        "import unittest\\nfrom app import add, mul\\nclass T(unittest.TestCase):\\n"
        "    def test_add(self): self.assertEqual(add(1,2), 3)\\n"
        "    def test_mul(self): self.assertEqual(mul(2,3), 6)\\n",
        encoding="utf-8",
    )

res = {{
    "status": "completed",
    "summary": f"Implemented mul() on call {{calls}}",
    "tests_run": ["python3 -m unittest test_app.py"],
    "discovered_issues": ["Consider adding div() later"],
    "blockers": "",
}}
finish(res)
""",
        )

        # Fake agy shim: verifies --mode plan on review and returns structured JSON with conversation_id
        _write_exec(
            self.fake_bin / "agy",
            """#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path

assert os.environ.get("AGENT_DISPATCH_CHILD") == "1", "Missing anti-nesting env"
assert "LINEAR_API_KEY" not in os.environ, "LINEAR_API_KEY leaked to agy!"
assert "CODEX_SUB2API_API_KEY" not in os.environ, "CODEX key leaked to agy!"
assert "--mode" in sys.argv and "plan" in sys.argv, f"Expected --mode plan for agy reviewer: {sys.argv}"

prompt_arg = next(a for a in sys.argv if a.startswith("--print="))
m = re.search(r"in (\\S+prompt-(standards|spec)-r\\d+\\.md)", prompt_arg)
axis = m.group(2) if m else "standards"
structured = {
    "axis": axis,
    "verdict": "pass",
    "hard_findings": [],
    "suggestions": [],
    "test_integrity_violation": False,
    "summary": f"{axis} axis passed.",
}
wrapper = {
    "conversation_id": f"aaaaaaaa-bbbb-cccc-dddd-00000000000{'1' if axis == 'standards' else '2'}",
    "status": "SUCCESS",
    "duration_seconds": 0.2,
    "structured_output": structured,
    "usage": {"total_tokens": 100},
}
print(json.dumps(wrapper))
""",
        )

        # Fake gh shim recording PR state and call log to self.gh_state_file / gh_calls.log
        self.gh_calls_log = self.root / "gh_calls.log"
        _write_exec(
            self.fake_bin / "gh",
            f"""#!/usr/bin/env python3
import fcntl, json, subprocess, sys
from pathlib import Path

db = Path("{self.gh_state_file}")
calls_log = Path("{self.gh_calls_log}")
db_lock = db.with_suffix(".lock").open("a")
fcntl.flock(db_lock, fcntl.LOCK_EX)
prs = json.loads(db.read_text())
args = sys.argv[1:]
with calls_log.open("a", encoding="utf-8") as fh:
    fh.write(" ".join(args[:4]) + "\\n")

if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1] if "--head" in args else ""
    state = args[args.index("--state") + 1] if "--state" in args else "open"
    matched = [p for p in prs if p["head"] == head and (state == "all" or p["state"].lower() == state.lower())]
    print(json.dumps(matched))
    sys.exit(0)
if args[:2] == ["pr", "view"]:
    num = int(args[2])
    found = next((p for p in prs if p["number"] == num), None)
    if not found:
        sys.exit(1)
    print(json.dumps(found))
    sys.exit(0)
if args[:2] == ["pr", "create"]:
    head = args[args.index("--head") + 1]
    base = args[args.index("--base") + 1]
    title = args[args.index("--title") + 1]
    body = Path(args[args.index("--body-file") + 1]).read_text()
    ref = f"refs/heads/{{head}}"
    sha = subprocess.run(["git", "ls-remote", "--exit-code", "origin", ref], text=True,
                         capture_output=True, check=True).stdout.split()[0]
    pr = {{
        "number": len(prs) + 1,
        "url": f"https://github.com/test/demo-repo/pull/{{len(prs)+1}}",
        "head": head,
        "headRefName": head,
        "isCrossRepository": False,
        "baseRefName": base,
        "title": title,
        "body": body,
        "state": "OPEN",
        "isDraft": "--draft" in args,
        "headRefOid": sha,
    }}
    prs.append(pr)
    db.write_text(json.dumps(prs))
    print(pr["url"])
    sys.exit(0)
if args[:2] == ["pr", "edit"]:
    num = int(args[2])
    for p in prs:
        if p["number"] == num:
            p["title"] = args[args.index("--title") + 1]
            p["body"] = args[args.index("--body") + 1]
    db.write_text(json.dumps(prs))
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    print("Logged in as test")
    sys.exit(0)
print(f"Unhandled fake gh command: {{args}}", file=sys.stderr)
sys.exit(1)
""",
        )

    def _run_cli(self, cli_args: list[str], *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        # Isolate from parent AGENT_DISPATCH_CHILD and host ~/.gitconfig!
        env.pop("AGENT_DISPATCH_CHILD", None)
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        env["LINEAR_API_KEY"] = "lin_api_fake_e2e_key_1234567890"
        env["AGENT_DISPATCH_LINEAR_URL"] = self.linear_url
        env["AGENT_DISPATCH_CACHE_ROOT"] = str(self.cache_dir)
        env["AGENT_DISPATCH_FORCE_GH"] = "1"
        env["CODEX_SUB2API_API_KEY"] = "fake_sub2api"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(BIN_PATH), *cli_args],
            cwd=str(self.repo),
            text=True,
            capture_output=True,
            env=env,
        )

    def test_missing_origin_fails_preflight_before_claim_but_permits_local_run(self) -> None:
        self.linear_state.add_issue("TST-O", "Work", "## Touches\n- src", labels=["agent-ready"])
        _git(self.repo, "remote", "remove", "origin")
        result = self._run_cli(["run", "TST-O", "--allow-branch-only", "--skip-baseline"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires an origin", result.stderr)
        self.assertFalse(self.linear_state.mutation_log)
        self.assertFalse((self.root / "codex_calls.count").exists())
        local = self._run_cli(["run", "TST-O", "--no-push", "--skip-baseline"])
        self.assertEqual(local.returncode, 0, local.stderr + local.stdout)
        self.assertEqual(json.loads(local.stdout)["status"], "passed_local")
        self.assertFalse(self.linear_state.mutation_log)

    def test_relative_origin_from_linked_worktree_only_updates_original_bare(self) -> None:
        iss = self.linear_state.add_issue("TST-REL", "Relative origin", "Work", labels=["agent-ready"])
        _git(self.repo, "remote", "set-url", "origin", f"../{self.bare.name}")
        wt_parent = self.cache_dir / "worktrees" / ad.repo_cache_key(self.repo)
        wt_parent.mkdir(parents=True)
        other = wt_parent / self.bare.name
        _git(wt_parent, "init", "--bare", "-b", "main", str(other))
        result = self._run_cli(["run", iss["identifier"], "--skip-baseline"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(_git(self.bare, "rev-parse", f"refs/heads/{output['branch']}"), output["commit"])
        self.assertFalse(_git(other, "for-each-ref", "refs/heads/"))
        resumed = self._run_cli(["resume", iss["identifier"]])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_legacy_local_checkpoint_cannot_publish_to_replaced_origin(self) -> None:
        iss = self.linear_state.add_issue("TST-OLD", "Local legacy delivery", "Work", labels=["agent-ready"])
        result = self._run_cli(["run", iss["identifier"], "--skip-baseline"], extra_env={"AGENT_DISPATCH_FORCE_GH": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state_file = Path(json.loads(result.stdout)["run_dir"]) / "state.json"
        state = json.loads(state_file.read_text())
        for key in ("publish_destination", "pre_push_sha"):
            state.pop(key)
        state.update(stage="verified_and_reviewed", pushed=False, push_attempted=False)
        state_file.write_text(json.dumps(state))
        iss["state"] = next(s for s in self.linear_state.states if s["name"] == "In Progress")
        other = self.root / "replacement.git"
        _git(self.root, "init", "--bare", "-b", "main", str(other))
        _git(self.repo, "remote", "set-url", "origin", str(other))
        result = self._run_cli(["resume", iss["identifier"], "--skip-project-check"], extra_env={"AGENT_DISPATCH_FORCE_GH": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot verify the publish destination", result.stderr)
        self.assertFalse(_git(other, "for-each-ref", "refs/heads/"))
        self.assertEqual(iss["state"]["name"], "In Progress")

    def test_branch_only_resume_requires_the_verified_base(self) -> None:
        issue = self.linear_state.add_issue("TST-BASE", "Resume delivery", "Work", labels=["agent-ready"])
        self.linear_state.fail_next_comment_create = True
        env = {"AGENT_DISPATCH_FORCE_GH": "0"}
        run = self._run_cli(["run", issue["identifier"]], extra_env=env)
        self.assertEqual(run.returncode, 1, run.stderr)
        state = json.loads((Path(json.loads(run.stdout)["run_dir"]) / "state.json").read_text())
        self.assertEqual(state["stage"], "linear_state_synced")
        base = _git(self.repo, "rev-parse", "HEAD")
        # A replacement base exists but has no common history with the checked base.
        replacement = _git(self.repo, "commit-tree", "HEAD^{tree}", "-m", "replacement")
        _git(self.repo, "push", "--force", "origin", f"{replacement}:refs/heads/main")
        mutations = len(self.linear_state.mutation_log)
        args = ["resume", issue["identifier"]]
        for missing in (False, True):
            if missing:
                _git(self.bare, "update-ref", "-d", "refs/heads/main")
            resumed = self._run_cli(args, extra_env=env)
            self.assertNotEqual(resumed.returncode, 0, resumed.stdout)
            self.assertIn("origin/main", resumed.stderr)
            self.assertEqual(len(self.linear_state.mutation_log), mutations)
        _git(self.repo, "commit", "--allow-empty", "-m", "normal advance")
        _git(self.repo, "push", "origin", "main")
        self.assertEqual(state["target_sha"], base)
        for readonly in (False, True):
            resumed = self._run_cli(args, extra_env=env)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            result = json.loads(resumed.stdout)
            self.assertEqual(result["status"], "passed_branch_only")
            self.assertEqual(result.get("verified_readonly", False), readonly)

    def test_review_explicit_base_in_local_trunk_repo(self) -> None:
        _git(self.repo, "remote", "remove", "origin")
        _git(self.repo, "branch", "-m", "trunk")
        _git(self.repo, "switch", "-c", "feature")
        (self.repo / "app.py").write_text("# feature\n")
        _git(self.repo, "commit", "-am", "feature")
        result = self._run_cli(["review", "--base", "trunk"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["base_sha"], _git(self.repo, "rev-parse", "trunk"))

    def test_run_fetches_current_base_with_restricted_refspec(self) -> None:
        issue = self.linear_state.add_issue("TST-REF", "Feature", "Work", labels=["agent-ready"])
        original = _git(self.repo, "rev-parse", "origin/main")
        _git(self.repo, "config", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other")
        _git(self.repo, "commit", "--allow-empty", "-m", "advance main")
        _git(self.repo, "push", "origin", "main")
        self.assertEqual(_git(self.repo, "rev-parse", "origin/main"), original)
        result = self._run_cli(["run", issue["identifier"], "--no-push"])
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = json.loads(result.stdout)["commit"]
        self.assertEqual(_git(self.repo, "rev-parse", f"{candidate}^"), _git(self.bare, "rev-parse", "main"))

    def test_remote_feature_deletion_during_preflight_is_preserved(self) -> None:
        issue = self.linear_state.add_issue("TST-MOVE", "Feature", "Work", labels=["agent-ready"])
        ref = "refs/heads/feat/tst-move"
        _git(self.repo, "push", "origin", f"HEAD:{ref}")
        setup = f'git --git-dir="$TEST_BARE" update-ref -d {ref}'
        result = self._run_cli(["run", issue["identifier"], "--setup-cmd", setup],
                               extra_env={"TEST_BARE": str(self.bare)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed during preflight", result.stderr)
        self.assertFalse(self.linear_state.mutation_log)
        self.assertFalse(_git(self.bare, "for-each-ref", ref))

    def test_cleanup_preserves_human_commit_during_merge_lookup(self) -> None:
        self.linear_state.add_issue("TST-KEEP", "Race cleanup", "Work", labels=["agent-ready"])
        result = self._run_cli(["run", "TST-KEEP", "--skip-baseline"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        wt = Path(output["worktree"])
        _write_exec(self.fake_bin / "gh", f"""#!/usr/bin/env python3
import json, subprocess
subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "new human work"],
               cwd={str(wt)!r}, check=True, stdout=subprocess.DEVNULL)
print(json.dumps([{{"number": 1, "headRefOid": {output['commit']!r}, "baseRefName": "main"}}]))
""")
        clean = self._run_cli(["cleanup"])
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertFalse(json.loads(clean.stdout)["removed"])
        self.assertTrue((wt / ".git").exists())
        self.assertEqual(_git(self.repo, "log", "-1", "--format=%s", output["branch"]), "new human work")

        # A ref can also move after Git has removed its worktree but before conditional deletion.
        old = _git(self.repo, "rev-parse", output["branch"])
        new = _git(self.repo, "commit-tree", f"{old}^{{tree}}", "-p", old, "-m", "late human commit")
        real_git = shutil.which("git")
        _write_exec(self.fake_bin / "git", f"""#!/usr/bin/env python3
import subprocess, sys
args = sys.argv[1:]
rc = subprocess.run([{real_git!r}, *args]).returncode
if rc == 0 and "worktree" in args and args[args.index("worktree") + 1] == "remove":
    subprocess.run([{real_git!r}, "-c", "core.hooksPath=/dev/null", "update-ref",
                    {"refs/heads/" + output["branch"]!r}, {new!r}, {old!r}], check=True)
sys.exit(rc)
""")
        clean = self._run_cli(["cleanup", "--all"])
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertFalse(wt.exists())
        self.assertFalse(json.loads(clean.stdout)["removed"])
        self.assertEqual(_git(self.repo, "rev-parse", output["branch"]), new)

    def test_mismatched_push_destination_aborts_before_claim(self) -> None:
        self.linear_state.add_issue("TST-DEST", "Destination", "hello")
        unrelated = self.root / "other.git"
        _git(self.root, "init", "--bare", str(unrelated))
        _git(self.repo, "remote", "set-url", "--push", "origin", str(unrelated))
        run = self._run_cli(["run", "TST-DEST", "--repo", str(self.repo)])
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("destinations", run.stderr)
        self.assertEqual(self.linear_state.mutation_log, [])
        self.assertEqual(_git(unrelated, "for-each-ref"), "")

    def test_claim_respects_cancellation_during_standards_snapshot(self) -> None:
        issue = self.linear_state.add_issue("TST-LATECLAIM", "Work", "Implement a change")
        real_git = shutil.which("git")
        marker = self.root / "standards-read"
        _write_exec(self.fake_bin / "git", (
            "#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\n"
            f"if 'ls-tree' in sys.argv: Path({str(marker)!r}).touch()\n"
            f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        ))
        handle = self.linear_state.handle

        def cancel_after_snapshot(query, variables):
            if marker.exists():
                marker.unlink()
                issue["state"] = next(s for s in self.linear_state.states if s["name"] == "Canceled")
            return handle(query, variables)

        with mock.patch.object(self.linear_state, "handle", side_effect=cancel_after_snapshot):
            result = self._run_cli(["run", issue["identifier"], "--skip-baseline"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(issue["state"]["name"], "Canceled")
        self.assertEqual(self.linear_state.mutation_log, [])
        latest = self.cache_dir / "runs" / ad.repo_cache_key(self.repo) / issue["identifier"] / "latest.json"
        self.assertNotEqual(json.loads(latest.read_text())["stage"], "claim_pending")

    def test_existing_pr_closed_during_worker_is_preserved_before_push(self) -> None:
        issue = self.linear_state.add_issue("TST-CLOSE", "Work", "Implement a change")
        head = _git(self.repo, "rev-parse", "HEAD")
        branch = "feat/tst-close"
        _git(self.repo, "push", "origin", f"HEAD:refs/heads/{branch}")
        pr = {"number": 7, "url": "https://example.invalid/pr/7", "head": branch, "headRefName": branch,
              "isCrossRepository": False, "headRefOid": head, "baseRefName": "main", "state": "OPEN",
              "isDraft": True, "body": "Human notes"}
        self.gh_state_file.write_text(json.dumps([pr]))
        codex = self.fake_bin / "codex"
        worker = codex.rename(self.fake_bin / "original-codex")
        _write_exec(codex, (
            "#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\n"
            f"db = Path({str(self.gh_state_file)!r})\nprs = json.loads(db.read_text())\n"
            "prs[0]['state'] = 'CLOSED'\ndb.write_text(json.dumps(prs))\n"
            f"os.execv({str(worker)!r}, [{str(worker)!r}, *sys.argv[1:]])\n"
        ))
        result = self._run_cli(["run", issue["identifier"], "--skip-baseline"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "publish_incomplete")
        self.assertEqual(_git(self.bare, "rev-parse", branch), head)
        self.assertEqual(json.loads(self.gh_state_file.read_text()), [{**pr, "state": "CLOSED"}])
        self.assertNotIn("pr create", self.gh_calls_log.read_text())

    def test_existing_non_draft_pr_fails_preflight_before_claim_or_worker(self) -> None:
        issue = self.linear_state.add_issue("TST-READY", "Work", "Implement a change")
        head = _git(self.repo, "rev-parse", "HEAD")
        branch = "feat/tst-ready"
        _git(self.repo, "push", "origin", f"HEAD:refs/heads/{branch}")
        pr = {"number": 9, "url": "https://example.invalid/pr/9", "head": branch, "headRefName": branch,
              "isCrossRepository": False, "headRefOid": head, "baseRefName": "main", "state": "OPEN",
              "isDraft": False, "body": "Ready PR"}
        self.gh_state_file.write_text(json.dumps([pr]))
        result = self._run_cli(["run", issue["identifier"], "--skip-baseline"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a Draft PR", result.stderr)
        self.assertEqual(self.linear_state.mutation_log, [])
        self.assertFalse((self.root / "codex_calls.count").exists())

    def test_resume_interrupted_claim_only_restores_owned_state(self) -> None:
        issue = self.linear_state.add_issue("TST-CLAIM", "Claim", "scope", state_name="In Progress")
        ticket_dir = self.cache_dir / "runs" / ad.repo_cache_key(self.repo) / issue["identifier"]
        run_dir = ticket_dir / "claim-run"
        run_dir.mkdir(parents=True)
        state = {"stage": "claim_pending", "issue_uuid": issue["id"], "run_dir": str(run_dir),
                 "pre_claim_state_id": "st-todo", "claimed_state_id": "st-prog",
                 "claimed_ownership": {"description_sha": ad.sha256_text("scope"), "assignee_id": ""}}
        ad.save_checkpoint(run_dir, ticket_dir / "latest.json", state)
        args = ["resume", issue["identifier"], "--repo", str(self.repo)]
        issue["assignee"] = {"id": "human", "name": "Human"}
        self.assertNotEqual(self._run_cli(args).returncode, 0)
        self.assertEqual(self.linear_state.mutation_log, [])
        issue["assignee"] = None
        restored = self._run_cli(args)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(issue["state"]["name"], "Todo")
        self.assertIn("dispatch:failed", [label["name"] for label in issue["_labels"]])
        count = len(self.linear_state.mutation_log)
        self.assertEqual(self._run_cli(args).returncode, 0)
        self.assertEqual(len(self.linear_state.mutation_log), count)

    def test_resume_preserves_human_changes_at_early_publish_stages(self) -> None:
        issue = self.linear_state.add_issue("TST-PUB", "Publish", "Add multiply", labels=["agent-ready"])
        self.linear_state.fail_next_comment_create = True
        first = self._run_cli(["run", issue["identifier"], "--repo", str(self.repo), "--skip-baseline"])
        self.assertEqual(first.returncode, 1, first.stderr)
        run_dir = Path(json.loads(first.stdout)["run_dir"])
        latest = run_dir.parent / "latest.json"
        state = json.loads(latest.read_text())
        self.assertEqual(state["stage"], "linear_state_synced")
        args = ["resume", issue["identifier"], "--repo", str(self.repo)]
        state["stage"] = "pr_ready"
        ad.save_checkpoint(run_dir, latest, state)
        issue["state"] = self.linear_state.states[1]  # Todo
        count = len(self.linear_state.mutation_log)
        self.assertNotEqual(self._run_cli(args).returncode, 0)
        self.assertEqual(len(self.linear_state.mutation_log), count)
        issue["state"] = self.linear_state.states[2]  # In Progress
        state["stage"] = "pushed"
        prs = json.loads(self.gh_state_file.read_text())
        prs[0].update(body="Human notes", state="CLOSED")
        self.gh_state_file.write_text(json.dumps(prs))
        state.pop("pr_number")  # create succeeded but its response was lost
        ad.save_checkpoint(run_dir, latest, state)
        self.assertNotEqual(self._run_cli(args).returncode, 0)
        self.assertEqual(json.loads(self.gh_state_file.read_text()), prs)
        prs[0]["state"] = "OPEN"
        self.gh_state_file.write_text(json.dumps(prs))
        _git(self.bare, "update-ref", "-d", f"refs/heads/{state['branch']}")
        self.assertNotEqual(self._run_cli(args).returncode, 0)
        self.assertEqual(_git(self.bare, "for-each-ref", f"refs/heads/{state['branch']}"), "")
        _git(self.bare, "update-ref", f"refs/heads/{state['branch']}", state["candidate_sha"])
        state["review"]["passed"] = False
        ad.save_checkpoint(run_dir, latest, state)
        self.assertNotEqual(self._run_cli(args).returncode, 0)
        self.assertEqual(len(self.linear_state.mutation_log), count)
        state["review"]["passed"] = True
        ad.save_checkpoint(run_dir, latest, state)
        success = self._run_cli(args)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(self.gh_state_file.read_text())[0]["body"], "Human notes")

    def test_nested_dispatch_is_blocked(self) -> None:
        res = self._run_cli(["doctor"], extra_env={"AGENT_DISPATCH_CHILD": "1"})
        self.assertEqual(res.returncode, 2)
        self.assertIn("Nested agent-dispatch invocation blocked", res.stderr)

    def test_e2e_publish_comment_failure_then_resume_reconciliation(self) -> None:
        """Runs REAL base baseline (no --skip-baseline!), verifies step 4 comment failure + resume + read-only completed resume!"""
        iss = self.linear_state.add_issue(
            "TST-10",
            "Add multiply",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        # Inject failure ONLY on step 4 (delivery summary commentCreate)
        self.linear_state.fail_next_comment_create = True

        # NO --skip-baseline! Proves baseline actually ran > 0 unittest cases!
        res1 = self._run_cli(["run", "TST-10", "--repo", str(self.repo)])
        self.assertEqual(res1.returncode, 1, res1.stderr + res1.stdout)
        out1 = json.loads(res1.stdout)
        self.assertEqual(out1["status"], "publish_incomplete")
        self.assertTrue(out1["pushed"])
        self.assertIn("https://github.com/test/demo-repo/pull/1", out1["pr_url"])
        # Confirm baseline verify genuinely ran 1 test (NOT 0 tests!)
        base_err_log = Path(out1["run_dir"]) / "baseline-verify.stderr.log"
        self.assertTrue(base_err_log.is_file())
        self.assertIn("Ran 1 test", base_err_log.read_text(encoding="utf-8"))
        # Linear MUST NOT have been rolled back to Todo!
        self.assertEqual(iss["state"]["name"], "In Review")
        self.assertIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
        self.assertNotIn("dispatch:failed", [l["name"] for l in iss["_labels"]])
        # Session traces persisted to sessions.jsonl
        sessions_jsonl = Path(out1["run_dir"]) / "sessions.jsonl"
        self.assertTrue(sessions_jsonl.is_file())
        self.assertEqual(len(out1["sessions"]), 3)  # 1 worker + 2 review axes

        # Now run `resume TST-10` -> MUST reconcile without rerunning codex!
        codex_calls_before = int((self.root / "codex_calls.count").read_text())
        res2 = self._run_cli(["resume", "TST-10", "--repo", str(self.repo)])
        self.assertEqual(res2.returncode, 0, res2.stderr + res2.stdout)
        out2 = json.loads(res2.stdout)
        self.assertEqual(out2["status"], "passed")
        self.assertTrue(out2["reconciled"])
        # Worker was NOT rerun!
        self.assertEqual(int((self.root / "codex_calls.count").read_text()), codex_calls_before)
        # Linear is In Review and failure label was cleared atomically!
        self.assertEqual(iss["state"]["name"], "In Review")
        self.assertNotIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
        # Re-running resume a second time on completed checkpoint is strictly READ-ONLY (zero pr edit/create/comment calls)!
        cmts_before = len(self.linear_state.comments[iss["id"]])
        gh_log_before = self.gh_calls_log.read_text(encoding="utf-8")
        res3 = self._run_cli(["resume", "TST-10", "--repo", str(self.repo)])
        self.assertEqual(res3.returncode, 0, res3.stderr)
        out3 = json.loads(res3.stdout)
        self.assertTrue(out3.get("verified_readonly"))
        self.assertEqual(len(self.linear_state.comments[iss["id"]]), cmts_before)
        new_gh_calls = self.gh_calls_log.read_text(encoding="utf-8")[len(gh_log_before):]
        self.assertNotIn("pr edit", new_gh_calls)
        self.assertNotIn("pr create", new_gh_calls)

        # If a human closes the PR on a completed checkpoint, resume MUST refuse to recreate it!
        prs = json.loads(self.gh_state_file.read_text())
        prs[0]["state"] = "CLOSED"
        self.gh_state_file.write_text(json.dumps(prs))
        res4 = self._run_cli(["resume", "TST-10", "--repo", str(self.repo)])
        self.assertEqual(res4.returncode, 2)
        self.assertIn("human action preserved", res4.stderr)

    def test_e2e_protected_path_triggers_rework_then_passes_and_cleanup_safety(self) -> None:
        self.linear_state.add_issue(
            "TST-20",
            "Multiply with rework",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        res = self._run_cli(
            ["run", "TST-20", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "tamper_gate_then_fix"},
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        out = json.loads(res.stdout)
        self.assertEqual(out["status"], "passed")
        self.assertEqual(out["attempt"], 2)  # Attempt 1 blocked by protected Makefile, attempt 2 succeeded!
        # Deduplicated discovered_issues across 2 attempts (only 1 entry, NOT duplicated!)
        self.assertEqual(len(out["discovered_issues"]), 1)

        # Simulate historical merged PR with OLD SHA, then add a new local commit on the branch -> cleanup MUST hit gh pr list AND NOT delete!
        prs = json.loads(self.gh_state_file.read_text())
        prs[0]["state"] = "MERGED"
        prs[0]["headRefOid"] = "0" * 40  # old merged SHA != current worktree HEAD
        self.gh_state_file.write_text(json.dumps(prs))

        wt = Path(out["worktree"])
        (wt / "notes.txt").write_text("new unpushed work\n", encoding="utf-8")
        _git(wt, "add", "notes.txt")
        _git(wt, "commit", "-m", "new unpushed commit")

        gh_log_before = self.gh_calls_log.read_text(encoding="utf-8")
        clean_res = self._run_cli(["cleanup", "--repo", str(self.repo)])
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr)
        clean_out = json.loads(clean_res.stdout)
        self.assertEqual(clean_out["removed"], [])
        self.assertTrue(wt.exists(), "cleanup wrongly deleted worktree with unpushed commits!")
        self.assertIn("pr list", self.gh_calls_log.read_text(encoding="utf-8")[len(gh_log_before):])

        # Removing owner.json + corrupting latest.json -> cleanup --all MUST SKIP (never fall back to unlocked delete!)
        owner_f = wt / ".dispatch" / "owner.json"
        if owner_f.is_file():
            owner_f.unlink()
        rkey = ad.repo_cache_key(self.repo)
        (self.cache_dir / "runs" / rkey / "TST-20" / "latest.json").write_text("{corrupt", encoding="utf-8")
        clean_all = self._run_cli(["cleanup", "--all", "--repo", str(self.repo)])
        self.assertEqual(clean_all.returncode, 0, clean_all.stderr)
        clean_all_out = json.loads(clean_all.stdout)
        self.assertEqual(clean_all_out["removed"], [])
        self.assertTrue(any("refusing unsafe delete" in s for s in clean_all_out["skipped"]), clean_all_out)
        self.assertTrue(wt.exists(), "cleanup --all MUST NOT delete worktree without verified lock owner!")

    def test_e2e_human_cancel_preserved_and_verify_untracked_cleaned_and_title_secret_blocked(self) -> None:
        # 1. Verify command producing untracked `coverage.xml` MUST be cleaned cleanly without polluting commit or failing review!
        self.linear_state.add_issue(
            "TST-40",
            "Verify untracked cleanup",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        res_cov = self._run_cli(
            [
                "run",
                "TST-40",
                "--repo",
                str(self.repo),
                "--skip-baseline",
                "--no-push",
                "--verify-cmd",
                "python3 -m unittest test_app.py && echo '<cov/>' > coverage.xml",
            ]
        )
        self.assertEqual(res_cov.returncode, 0, res_cov.stderr + res_cov.stdout)
        out_cov = json.loads(res_cov.stdout)
        self.assertEqual(out_cov["status"], "passed_local")
        self.assertFalse((Path(out_cov["worktree"]) / "coverage.xml").exists())

        # 2. Secret in Linear ticket title MUST be blocked BEFORE calling any LLM harness (0 codex calls!)
        codex_before_sec = int((self.root / "codex_calls.count").read_text()) if (self.root / "codex_calls.count").exists() else 0
        iss_sec = self.linear_state.add_issue(
            "TST-41",
            "Fix token github_pat_11ABCDEFG01234567890_abcdefghijklmnop leak",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        res_sec = self._run_cli(["run", "TST-41", "--repo", str(self.repo), "--skip-baseline"])
        self.assertEqual(res_sec.returncode, 1, res_sec.stderr + res_sec.stdout)
        out_sec = json.loads(res_sec.stdout)
        self.assertEqual(out_sec["status"], "secret_in_metadata")
        self.assertEqual(iss_sec["state"]["name"], "Todo")
        codex_after_sec = int((self.root / "codex_calls.count").read_text()) if (self.root / "codex_calls.count").exists() else 0
        self.assertEqual(codex_after_sec, codex_before_sec, "Title secret MUST fail BEFORE invoking worker harness!")

        # 3. Human cancels ticket mid-run -> finally MUST preserve Canceled (NEVER overwrite back to Todo)!
        iss_can = self.linear_state.add_issue(
            "TST-42",
            "Cancel mid-run",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        canceled_st = next(s for s in self.linear_state.states if s["name"] == "Canceled")
        orig_handle = self.linear_state.handle

        def cancel_after_claim(q: str, v: dict[str, Any]) -> dict[str, Any]:
            resp = orig_handle(q, v)
            if "mutation" in q and "issueUpdate" in q and v.get("id") == iss_can["id"] and v.get("stateId") == "st-prog":
                iss_can["state"] = canceled_st
                iss_can["updatedAt"] = "2026-11-24T23:59:59Z"
            return resp

        self.linear_state.handle = cancel_after_claim  # type: ignore[method-assign]
        try:
            res_can = self._run_cli(["run", "TST-42", "--repo", str(self.repo), "--skip-baseline"])
        finally:
            self.linear_state.handle = orig_handle  # type: ignore[method-assign]
        self.assertNotEqual(res_can.returncode, 0)
        self.assertEqual(iss_can["state"]["name"], "Canceled", "finally wrongly overwrote human Canceled state!")
        self.assertNotIn("dispatch:failed", [l["name"] for l in iss_can["_labels"]])

        # 4. CLI rejects non-positive --max-parallel, --limit, and out-of-range --max-rework with exact error text!
        bad_par = self._run_cli(["frontier", "--max-parallel", "0", "--repo", str(self.repo)])
        self.assertEqual(bad_par.returncode, 2)
        self.assertIn("--max-parallel", bad_par.stderr)
        bad_lim = self._run_cli(["frontier", "--limit", "-1", "--repo", str(self.repo)])
        self.assertEqual(bad_lim.returncode, 2)
        self.assertIn("--limit", bad_lim.stderr)
        bad_rw = self._run_cli(["frontier", "--max-rework", "9", "--repo", str(self.repo)])
        self.assertEqual(bad_rw.returncode, 2)
        self.assertIn("--max-rework", bad_rw.stderr)

    def test_overlapping_specs_reserve_touches_before_linear_claim(self) -> None:
        for num, resource in ((1, "src/api"), (2, "src/api/v1")):
            parent = self.linear_state.add_issue(f"SPEC-{num}", "Spec", "", state_name="Backlog")
            child = self.linear_state.add_issue(f"LEAF-{num}", "Work", f"## Touches\n- {resource}\n", labels=["agent-ready"])
            parent["_children"] = [{**child, "labels": {"nodes": child["_labels"], "pageInfo": {"hasNextPage": False}},
                                    "children": {"nodes": []}}]
        entered, release = self.root / "entered", self.root / "release"
        barrier = self.root / "setup.py"
        barrier.write_text(
            f"from pathlib import Path\nimport time\nPath({str(entered)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n"
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self._run_cli, ["spec", "SPEC-1", "--skip-baseline", "--setup-cmd", f"python3 {barrier}"])
            try:
                deadline = time.monotonic() + 10
                while not entered.exists() and not first.done() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(entered.exists(), "first ticket never reached pre-claim setup")
                second = self._run_cli(["spec", "SPEC-2", "--skip-baseline"])
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(json.loads(second.stdout)["wave_dispatched"], 0)
                self.assertEqual(self.linear_state.get_issue("LEAF-2")["state"]["name"], "Todo")
            finally:
                release.touch()
            one = first.result(timeout=30)
            self.assertEqual(one.returncode, 0, one.stderr + one.stdout)
        # The local lock is released. The other parent's In Review ticket still owns this resource.
        retry = self._run_cli(["spec", "SPEC-2", "--skip-baseline"])
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["wave_dispatched"], 0)

    def test_cleanup_reclaims_abandoned_baseline_after_lock_release(self) -> None:
        # Exit inside the baseline context without finally. Keep its lock live for the first cleanup.
        base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "--allow-empty", "-m", "ahead")
        entered, release = self.root / "entered", self.root / "release"
        runner = self.root / "baseline.py"
        runner.write_text(
            "import os, runpy, time\nfrom pathlib import Path\n"
            f"ad=runpy.run_path({str(BIN_PATH)!r}); repo=Path({str(self.repo)!r})\n"
            "def interrupt(*args, **kwargs):\n"
            f"    Path({str(entered)!r}).touch()\n"
            f"    while not Path({str(release)!r}).exists(): time.sleep(.02)\n"
            "    os._exit(42)\n"
            "ad['check_base_baseline'].__globals__['bootstrap_worktree']=interrupt\n"
            f"ad['check_base_baseline'](repo, ad['repo_cache_key'](repo), repo, {base!r}, 'true', repo)\n"
        )
        env = {**os.environ, "AGENT_DISPATCH_CACHE_ROOT": str(self.cache_dir)}
        with subprocess.Popen([sys.executable, "-B", str(runner)], env=env) as child:
            try:
                deadline = time.monotonic() + 10
                while not entered.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(entered.exists())
                self.assertFalse(json.loads(self._run_cli(["cleanup", "--all"]).stdout)["removed"])
            finally:
                release.touch()
            self.assertEqual(child.wait(timeout=10), 42)
        output = self._run_cli(["cleanup"])
        self.assertEqual(output.returncode, 0, output.stderr)
        removed = json.loads(output.stdout)["removed"]
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0].startswith(".baseline-"))
        self.assertNotIn(".baseline-", _git(self.repo, "worktree", "list"))

    def test_doctor_checks_repo_and_linear_mapping(self) -> None:
        ok = self._run_cli(["doctor"])
        self.assertEqual(ok.returncode, 0, ok.stderr + ok.stdout)
        self.assertTrue(json.loads(ok.stdout)["ok"])
        missing = self._run_cli(["doctor", "--project", "missing-project"])
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(json.loads(missing.stdout)["ok"])
        local_bin = Path.home() / ".local/bin"
        local_bin.mkdir(parents=True)
        codex = (self.fake_bin / "codex").rename(local_bin / "codex")
        for tool in ("python3", "git"):
            (self.fake_bin / tool).symlink_to(shutil.which(tool))
        isolated_path = {"PATH": str(self.fake_bin)}
        result = self._run_cli(["doctor"], extra_env=isolated_path)
        harnesses = json.loads(result.stdout)["checks"]["harnesses"]
        self.assertEqual(harnesses["codex"], {"ok": True, "path": str(codex)})
        codex.chmod(0o644)
        result = self._run_cli(["doctor"], extra_env=isolated_path)
        self.assertFalse(json.loads(result.stdout)["checks"]["harnesses"]["codex"]["ok"])

    def test_setup_helper_available_during_checks_and_excluded_from_candidate(self) -> None:
        self.linear_state.add_issue("TST-HELP", "feature", "## Touches\n- app", labels=["agent-ready"])
        result = self._run_cli([
            "run", "TST-HELP", "--no-push", "--setup-cmd", "echo setup > 'helper[*]'",
            "--verify-cmd", "test -f 'helper[*]' && make verify",
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        out = json.loads(result.stdout)
        wt = Path(out["worktree"])
        self.assertNotIn("helper[*]", _git(wt, "ls-tree", "-r", "--name-only", out["commit"]).splitlines())
        self.assertEqual(_git(wt, "status", "--porcelain"), "")

    def test_setup_cannot_change_candidate_source_before_claim(self) -> None:
        for n, setup in enumerate(("echo setup >> app.py", "git commit --allow-empty -m setup",
                                   "git switch --detach HEAD")):
            with self.subTest(setup=setup):
                ident = f"TST-{n + 100}"
                self.linear_state.add_issue(ident, "Feature", "Work", labels=["agent-ready"])
                result = self._run_cli(["run", ident, "--setup-cmd", setup, "--skip-baseline"])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Bootstrap changed", result.stderr)
                self.assertEqual(self.linear_state.mutation_log, [])
                self.assertFalse((self.root / "codex_calls.count").exists())

    def test_worker_changes_trigger_new_verification_recipe_on_each_candidate(self) -> None:
        issue = self.linear_state.add_issue("TST-CAND", "Backend checks", "## Touches\n- tests", labels=["agent-ready"])
        _git(self.repo, "rm", "Makefile", "test_app.py")
        (self.repo / "package.json").write_text('{"scripts":{"test":"echo frontend"}}')
        _git(self.repo, "add", "package.json")
        _git(self.repo, "commit", "-m", "start with frontend verification")
        _git(self.repo, "push", "origin", "main")
        _write_exec(self.fake_bin / "npm", "#!/bin/sh\necho frontend-check\n")
        _write_exec(self.fake_bin / "go", "#!/bin/sh\necho go-check-verified\n")
        result = self._run_cli(["run", issue["identifier"], "--no-push", "--max-rework", "1"],
                               extra_env={"FAKE_CODEX_MODE": "add_backend_checks"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["attempt"], 2)
        run_dir = Path(out["run_dir"])
        self.assertIn("first backend candidate", (run_dir / "verify-r1.stderr.log").read_text())
        self.assertIn("go-check-verified", (run_dir / "verify-r2.log").read_text())
        self.assertIn("go test", out["verify"]["command"])

    def test_verify_modified_setup_file_remains_untracked_through_rework(self) -> None:
        issue = self.linear_state.add_issue("TST-MUT", "feature", "## Touches\n- app", labels=["agent-ready"])
        result = self._run_cli([
            "run", issue["identifier"], "--no-push", "--skip-baseline", "--max-rework", "1",
            "--setup-cmd", "echo setup > helper.cache",
            "--verify-cmd", "grep -qx checked helper.cache || { echo checked >> helper.cache; exit 1; }",
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        out = json.loads(result.stdout)
        self.assertEqual(out["attempt"], 2)
        wt = Path(out["worktree"])
        self.assertNotIn("helper.cache", _git(wt, "ls-tree", "-r", "--name-only", out["commit"]).splitlines())
        self.assertEqual(_git(wt, "status", "--porcelain"), "?? helper.cache")
        self.assertEqual((wt / "helper.cache").read_text(), "setup\nchecked\n")

    def test_reused_feature_checks_current_base_with_base_verify_recipe(self) -> None:
        issue = self.linear_state.add_issue("TST-FORK", "feature", "## Touches\n- app", labels=["agent-ready"])
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(self.cache_dir)}):
            wt, _, _ = ad.prepare_worktree(self.repo, issue["identifier"], "main", issue_uuid=issue["id"])
        _git(wt, "commit", "--allow-empty", "-m", "feature before base moved")
        (self.repo / "Makefile").write_text("check:\n\t@echo current-base-red\n\t@false\n")
        _git(self.repo, "commit", "-am", "base now fails check")
        _git(self.repo, "push", "origin", "main")
        target = _git(self.repo, "rev-parse", "HEAD")
        result = self._run_cli(["run", issue["identifier"]])
        self.assertNotEqual(result.returncode, 0)
        out = json.loads(result.stdout)
        self.assertEqual(out["status"], "base_branch_red")
        self.assertEqual(out["base_sha"], target)
        self.assertEqual(out["verify"]["command"], "make check")
        self.assertIn("current-base-red", out["verify"]["output"])
        self.assertFalse(self.linear_state.mutation_log)

    def test_worktree_cache_ancestors_cannot_redirect_cleanup_resume_or_run(self) -> None:
        # An explicitly configured cache root may be a symlink.
        cache_alias = self.root / "cache-alias"
        self.cache_dir.mkdir(exist_ok=True)
        cache_alias.symlink_to(self.cache_dir, target_is_directory=True)
        env = {"AGENT_DISPATCH_CACHE_ROOT": str(cache_alias)}
        issue = self.linear_state.add_issue("TST-ARCH", "Archived worktree", "Work", labels=["agent-ready"])
        first = self._run_cli(["run", issue["identifier"], "--skip-baseline"], extra_env=env)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        wt = Path(json.loads(first.stdout)["worktree"])
        archive = self.root / "personal-archive"
        archive.mkdir()
        moved = archive / wt.name
        _git(self.repo, "worktree", "move", str(wt), str(moved))
        sentinel = moved / "archive-only.txt"
        sentinel.write_text("personal draft")
        wt.parent.rmdir()
        wt.parent.symlink_to(archive, target_is_directory=True)

        self.linear_state.add_issue("TST-NEW", "New ticket", "Work", labels=["agent-ready"])
        for args in (["cleanup", "--all"], ["resume", issue["identifier"]],
                     ["run", "TST-NEW", "--skip-baseline", "--no-push"]):
            with self.subTest(args=args):
                result = self._run_cli(args, extra_env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symlink", result.stderr.lower())
                self.assertEqual(sentinel.read_text(), "personal draft")
                self.assertFalse((archive / "TST-NEW").exists())

        # The same guard applies one level higher, including a dangling destination.
        wt.parent.unlink()
        worktrees = wt.parent.parent
        worktrees.rmdir()
        missing = archive / "missing"
        worktrees.symlink_to(missing, target_is_directory=True)
        result = self._run_cli(["run", "TST-NEW", "--no-push", "--skip-baseline"], extra_env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr.lower())
        self.assertFalse(missing.exists())

    def test_cleanup_requires_owner_and_checkpoint_agreement(self) -> None:
        self.linear_state.add_issue("TST-OWN", "Work", "## Touches\n- src", labels=["agent-ready"])
        result = self._run_cli(["run", "TST-OWN", "--no-push", "--skip-baseline"])
        self.assertEqual(result.returncode, 0, result.stderr)
        wt = Path(json.loads(result.stdout)["worktree"])
        owner_path = wt / ".dispatch" / "owner.json"
        data = json.loads(owner_path.read_text())
        _git(wt, "switch", "-c", "personal/notes")
        data["branch"] = "personal/notes"
        owner_path.write_text(json.dumps(data))
        self.assertEqual(json.loads(self._run_cli(["cleanup", "--all"]).stdout)["removed"], [])
        self.assertTrue((wt / ".git").is_file())

    def test_e2e_parallel_frontier_two_tickets_pass_and_cross_wave_touches_serialize(self) -> None:
        """Dispatch independent tickets together and defer conflicts with the following wave."""
        self.linear_state.add_issue(
            "TST-P1",
            "Parallel A",
            "## What to Build\nMod A\n\n## Touches\n- mod_a\n\n## Testing Seam & Acceptance Criteria\n- [ ] ok\n",
            labels=["agent-ready"],
        )
        self.linear_state.add_issue(
            "TST-P2",
            "Parallel B",
            "## What to Build\nMod B\n\n## Touches\n- mod_b\n\n## Testing Seam & Acceptance Criteria\n- [ ] ok\n",
            labels=["agent-ready"],
        )
        self.linear_state.add_issue(
            "TST-P3",
            "Overlaps A child path",
            "## What to Build\nMod A sub\n\n## Touches\n- mod_a/sub\n\n## Testing Seam & Acceptance Criteria\n- [ ] ok\n",
            labels=["agent-ready"],
        )

        res1 = self._run_cli(
            ["frontier", "--max-parallel", "2", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "parallel_safe"},
        )
        self.assertEqual(res1.returncode, 0, res1.stderr + res1.stdout)
        out1 = json.loads(res1.stdout)
        self.assertEqual(out1["dispatched"], 2)
        statuses = {r["issue"]: r["status"] for r in out1["results"]}
        self.assertEqual(statuses, {"TST-P1": "passed", "TST-P2": "passed"}, out1)
        self.assertEqual([d["id"] for d in out1["deferred_for_next_wave"]], ["TST-P3"])

        # Second frontier while TST-P1 is still `In Review` MUST STILL defer TST-P3 across waves!
        res2 = self._run_cli(
            ["frontier", "--max-parallel", "2", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "parallel_safe"},
        )
        self.assertEqual(res2.returncode, 0, res2.stderr + res2.stdout)
        out2 = json.loads(res2.stdout)
        self.assertEqual(out2["dispatched"], 0)
        self.assertEqual([d["id"] for d in out2["deferred_for_next_wave"]], ["TST-P3"])

    def test_e2e_real_os_sigterm_during_push_records_publish_incomplete_and_resumes(self) -> None:
        """Real OS SIGTERM sent to orchestrator during `git push`: MUST write dispatch:publish-incomplete + comment, then resume cleanly!"""
        iss = self.linear_state.add_issue(
            "TST-TERM",
            "Real SIGTERM test",
            "## What to Build\nAdd mul\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul ok\n",
            labels=["agent-ready"],
        )
        real_git = shutil.which("git")
        assert real_git is not None
        sig_marker = self.root / "sigterm_sent.flag"
        sig_bin = self.root / "sigbin"
        sig_bin.mkdir()
        # Wrapper `git` that executes real push and sends real SIGTERM to parent orchestrator once!
        _write_exec(
            sig_bin / "git",
            f"""#!/usr/bin/env python3
import os, signal, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
res = subprocess.run(["{real_git}", *args])
marker = Path("{sig_marker}")
if "push" in args and res.returncode == 0 and not marker.exists():
    marker.write_text("1", encoding="utf-8")
    os.kill(os.getppid(), signal.SIGTERM)
sys.exit(res.returncode)
""",
        )
        res1 = self._run_cli(
            ["run", "TST-TERM", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"PATH": f"{sig_bin}:{self.fake_bin}:{os.environ['PATH']}"},
        )
        self.assertTrue(sig_marker.exists(), f"Push did not reach SIGTERM injection (exit {res1.returncode}): {res1.stdout} {res1.stderr}")
        self.assertEqual(res1.returncode, 128 + signal.SIGTERM, res1.stderr + res1.stdout)
        # Checkpoint MUST persist pushed=True and stage='pushed' immediately once remote read-back confirms push!
        rkey = ad.repo_cache_key(self.repo)
        ckpt = json.loads((self.cache_dir / "runs" / rkey / "TST-TERM" / "latest.json").read_text(encoding="utf-8"))
        state_json = json.loads((Path(ckpt["run_dir"]) / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(ckpt["stage"], "pushed")
        self.assertTrue(ckpt["pushed"])
        self.assertEqual(state_json["stage"], "pushed")
        self.assertTrue(state_json["pushed"])
        # Linear MUST have dispatch:publish-incomplete + comment and MUST NOT have rolled back to Todo!
        self.assertEqual(iss["state"]["name"], "In Progress")
        self.assertIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
        self.assertNotIn("dispatch:failed", [l["name"] for l in iss["_labels"]])
        self.assertTrue(any("Publish Interrupted" in c["body"] and "confirmed" in c["body"] for c in self.linear_state.comments[iss["id"]]))

        # Maintainer deletes remote branch after seeing confirmed push -> `resume` MUST refuse and NOT recreate the branch!
        branch_ref = f"refs/heads/{ckpt['branch']}"
        pushed_sha = ckpt["candidate_sha"]
        _git(self.bare, "update-ref", "-d", branch_ref)
        self.assertEqual(_git(self.bare, "for-each-ref", branch_ref), "")
        res_del = self._run_cli(["resume", "TST-TERM", "--repo", str(self.repo)])
        self.assertNotEqual(res_del.returncode, 0)
        self.assertIn("human action preserved", res_del.stderr)
        self.assertEqual(_git(self.bare, "for-each-ref", branch_ref), "", "resume MUST NOT recreate human-deleted branch!")

        # Restore remote branch ref -> normal `resume` converges cleanly to passed!
        _git(self.bare, "update-ref", branch_ref, pushed_sha)
        res2 = self._run_cli(["resume", "TST-TERM", "--repo", str(self.repo)])
        self.assertEqual(res2.returncode, 0, res2.stderr + res2.stdout)
        out2 = json.loads(res2.stdout)
        self.assertEqual(out2["status"], "passed")
        self.assertEqual(iss["state"]["name"], "In Review")
        self.assertNotIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])

    def test_e2e_pre_publish_rollback_outage_persists_rollback_pending_and_resumes_safely(self) -> None:
        """Linear outage during pre-publish cleanup persists rollback_pending; resume unlocks owned issue without overwriting human edits."""
        iss = self.linear_state.add_issue(
            "TST-RB",
            "Outage during rollback",
            "## What to Build\nWork\n\n## Touches\n- src/auth\n",
            labels=["agent-ready"],
        )
        outage_flag = self.root / "linear_offline.flag"
        self.linear_state.outage_flag_file = outage_flag

        res_fail = self._run_cli(
            ["run", "TST-RB", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "crash_with_linear_outage", "FAKE_OUTAGE_FLAG": str(outage_flag)},
        )
        self.assertNotEqual(res_fail.returncode, 0)
        self.assertTrue(outage_flag.exists())
        # Ticket stayed In Progress because Linear was unreachable during cleanup; checkpoint recorded rollback_pending.
        self.assertEqual(iss["state"]["name"], "In Progress")
        rkey = ad.repo_cache_key(self.repo)
        ckpt = json.loads((self.cache_dir / "runs" / rkey / "TST-RB" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(ckpt["stage"], "rollback_pending")
        self.assertEqual(ckpt["pre_claim_state_id"], "st-todo")
        self.assertEqual(ckpt["claimed_state_id"], "st-prog")

        # Restore Linear connectivity.
        outage_flag.unlink()
        self.linear_state.outage_flag_file = None

        # Plain `run` directs user to `resume` instead of blindly rerunning.
        res_run = self._run_cli(["run", "TST-RB", "--repo", str(self.repo), "--skip-baseline"])
        self.assertNotEqual(res_run.returncode, 0)
        self.assertIn("resume TST-RB", res_run.stderr)

        # Human edits (Done / Canceled / Todo / assignee / description) MUST NEVER be overwritten by `resume`.
        orig_state = iss["state"]
        orig_desc = iss["description"]
        mut_before = len(self.linear_state.mutation_log)
        for label, mutate, restore in (
            ("Done", lambda: iss.__setitem__("state", next(s for s in self.linear_state.states if s["name"] == "Done")),
             lambda: iss.__setitem__("state", orig_state)),
            ("Canceled", lambda: iss.__setitem__("state", next(s for s in self.linear_state.states if s["name"] == "Canceled")),
             lambda: iss.__setitem__("state", orig_state)),
            ("Todo", lambda: iss.__setitem__("state", next(s for s in self.linear_state.states if s["name"] == "Todo")),
             lambda: iss.__setitem__("state", orig_state)),
            ("assignee", lambda: iss.__setitem__("assignee", {"id": "human-1", "name": "Alice"}),
             lambda: iss.__setitem__("assignee", None)),
            ("description", lambda: iss.__setitem__("description", orig_desc + "\nEdited by human"),
             lambda: iss.__setitem__("description", orig_desc)),
        ):
            with self.subTest(human_change=label):
                mutate()
                try:
                    res_guard = self._run_cli(["resume", "TST-RB", "--repo", str(self.repo)])
                    self.assertNotEqual(res_guard.returncode, 0)
                    self.assertEqual(len(self.linear_state.mutation_log), mut_before)
                finally:
                    restore()

        # With original ownership intact, `resume` adds `dispatch:failed` FIRST, reverts to `Todo`, and posts diagnostic comment.
        res_rec = self._run_cli(["resume", "TST-RB", "--repo", str(self.repo)])
        self.assertEqual(res_rec.returncode, 0, res_rec.stderr + res_rec.stdout)
        out_rec = json.loads(res_rec.stdout)
        self.assertEqual(out_rec["status"], "rollback_completed")
        self.assertEqual(iss["state"]["name"], "Todo")
        self.assertIn("dispatch:failed", [l["name"] for l in iss["_labels"]])
        new_muts = self.linear_state.mutation_log[mut_before:]
        lbl_idx = next(i for i, (k, d) in enumerate(new_muts) if k == "addLabels" and "dispatch:failed" in d["labels"])
        todo_idx = next(i for i, (k, d) in enumerate(new_muts) if k == "state" and d["state"] == "Todo")
        self.assertLess(lbl_idx, todo_idx)
        self.assertTrue(any("recovered via resume" in c["body"] for c in self.linear_state.comments[iss["id"]]))

        # Second `resume` is idempotent with zero additional mutations; ticket no longer blocks Touches in frontier.
        mut_after = len(self.linear_state.mutation_log)
        res_rec2 = self._run_cli(["resume", "TST-RB", "--repo", str(self.repo)])
        self.assertEqual(res_rec2.returncode, 0, res_rec2.stderr)
        self.assertEqual(len(self.linear_state.mutation_log), mut_after)

        self.linear_state.add_issue("TST-RB-SIB", "Sibling on auth", "## Touches\n- src/auth\n", labels=["agent-ready"])
        res_front = self._run_cli(["frontier", "--repo", str(self.repo), "--skip-baseline"])
        self.assertEqual(res_front.returncode, 0, res_front.stderr + res_front.stdout)
        out_front = json.loads(res_front.stdout)
        self.assertEqual([r["issue"] for r in out_front["results"]], ["TST-RB-SIB"])

    def test_e2e_bootstrap_disables_husky_and_blocks_shared_git_config_tampering(self) -> None:
        """Auto dependency bootstrap sets HUSKY=0 and aborts before claim if shared .git/config is modified (candidate & baseline)."""
        (self.repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
        (self.repo / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
        _git(self.repo, "add", "package.json", "package-lock.json")
        _git(self.repo, "commit", "-m", "add node lockfile")
        _git(self.repo, "push", "origin", "main")

        husky_log = self.root / "husky_env.log"
        _write_exec(
            self.fake_bin / "npm",
            f"""#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
Path({str(husky_log)!r}).write_text(os.environ.get("HUSKY", ""), encoding="utf-8")
if os.environ.get("FAKE_NPM_TAMPER_GIT_CONFIG") == "1":
    subprocess.run(["git", "config", "core.hooksPath", ".husky/_"], check=True)
(Path.cwd() / "node_modules").mkdir(exist_ok=True)
""",
        )

        # 1. Candidate worktree bootstrap tampering shared core.hooksPath MUST abort before Linear claim!
        self.linear_state.add_issue("TST-BOOT1", "Node bootstrap tamper", "## Touches\n- app\n", labels=["agent-ready"])
        res_tamper = self._run_cli(
            ["run", "TST-BOOT1", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_NPM_TAMPER_GIT_CONFIG": "1"},
        )
        self.assertNotEqual(res_tamper.returncode, 0)
        self.assertIn("shared git config", res_tamper.stderr)
        self.assertEqual(husky_log.read_text(encoding="utf-8"), "0")
        self.assertEqual(self.linear_state.mutation_log, [])
        self.assertFalse((self.root / "codex_calls.count").exists())
        # Restore shared git config after simulated tamper so subsequent assertions start clean.
        _git(self.repo, "config", "--unset", "core.hooksPath", check=False)

        # 2. Detached baseline bootstrap tampering shared core.hooksPath MUST ALSO abort before Linear claim!
        iss_base = self.linear_state.add_issue("TST-BOOT2", "Baseline bootstrap tamper", "## Touches\n- app\n", labels=["agent-ready"])
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(self.cache_dir)}):
            wt2, _, _ = ad.prepare_worktree(self.repo, iss_base["identifier"], "main", issue_uuid=iss_base["id"])
        # Pre-populate node_modules on candidate so candidate skips npm ci, while feature branch is ahead so detached baseline runs npm ci!
        (wt2 / "node_modules").mkdir()
        _git(wt2, "commit", "--allow-empty", "-m", "ahead of base")
        res_base_tamper = self._run_cli(
            ["run", "TST-BOOT2", "--repo", str(self.repo)],
            extra_env={"FAKE_NPM_TAMPER_GIT_CONFIG": "1"},
        )
        self.assertNotEqual(res_base_tamper.returncode, 0)
        self.assertIn("shared git config", res_base_tamper.stderr)
        self.assertEqual(self.linear_state.mutation_log, [])
        self.assertFalse((self.root / "codex_calls.count").exists())
        _git(self.repo, "config", "--unset", "core.hooksPath", check=False)

        # 3. Clean auto bootstrap passes and saw HUSKY=0.
        self.linear_state.add_issue("TST-BOOT3", "Clean node bootstrap", "## Touches\n- app\n", labels=["agent-ready"])
        res_ok = self._run_cli(["run", "TST-BOOT3", "--repo", str(self.repo), "--no-push", "--skip-baseline"])
        self.assertEqual(res_ok.returncode, 0, res_ok.stderr + res_ok.stdout)
        self.assertEqual(husky_log.read_text(encoding="utf-8"), "0")

    def test_e2e_root_gitattributes_authorization_and_trusted_baseline_diff_scans(self) -> None:
        """Authorized root .gitattributes edits pass while unauthorized edits and .git/config tampering fail; diff/secret scans use baseline attributes."""
        # 1. Unauthorized edit to root .gitattributes is blocked immediately.
        self.linear_state.add_issue(
            "TST-GA1", "Unauthorized gitattributes", "## What to Build\nUpdate attrs\n", labels=["agent-ready"]
        )
        res_unauth = self._run_cli(
            ["run", "TST-GA1", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "edit_gitattributes"},
        )
        self.assertNotEqual(res_unauth.returncode, 0)
        self.assertIn("Unauthorized .gitattributes tampering", res_unauth.stderr)

        # 2. Explicitly authorized via `- gate: .gitattributes` passes, AND reviewer diff still uses trusted baseline attributes!
        self.linear_state.add_issue(
            "TST-GA2",
            "Authorized gitattributes via brief",
            "## What to Build\nUpdate attrs\n\n## Authorized Gate/Test Changes\n- gate: .gitattributes\n",
            labels=["agent-ready"],
        )
        res_brief = self._run_cli(
            ["run", "TST-GA2", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "edit_gitattributes"},
        )
        self.assertEqual(res_brief.returncode, 0, res_brief.stderr + res_brief.stdout)
        out_brief = json.loads(res_brief.stdout)
        self.assertEqual(out_brief["status"], "passed")
        patch_txt = (Path(out_brief["run_dir"]) / "diff-r1.patch").read_text(encoding="utf-8")
        self.assertIn("+def mul(a, b):", patch_txt, "Reviewer diff must use baseline attributes and ignore worker `*.py -diff`!")
        self.assertNotIn("Binary files a/app.py and b/app.py differ", patch_txt)

        # 3. Explicitly authorized via CLI `--allow-gate-edits` also passes.
        self.linear_state.add_issue(
            "TST-GA3", "Authorized gitattributes via CLI", "## What to Build\nUpdate attrs\n", labels=["agent-ready"]
        )
        res_cli = self._run_cli(
            ["run", "TST-GA3", "--repo", str(self.repo), "--allow-gate-edits", "--no-push", "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "edit_gitattributes"},
        )
        self.assertEqual(res_cli.returncode, 0, res_cli.stderr + res_cli.stdout)
        self.assertEqual(json.loads(res_cli.stdout)["status"], "passed_local")

        # 4. Authorized `.gitattributes` adding `-diff` cannot hide secrets from pre-commit / range scans.
        self.linear_state.add_issue(
            "TST-GA4",
            "Authorized gitattributes secret hiding",
            "## Authorized Gate/Test Changes\n- gate: .gitattributes\n",
            labels=["agent-ready"],
        )
        res_sec = self._run_cli(
            ["run", "TST-GA4", "--repo", str(self.repo), "--max-rework", "0", "--no-push", "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "edit_gitattributes_with_secret"},
        )
        self.assertNotEqual(res_sec.returncode, 0)
        self.assertEqual(json.loads(res_sec.stdout)["status"], "failed_max_rework")

        # 5. Even with `- gate: .gitattributes` and `--allow-gate-edits`, `.git/config` tampering is ALWAYS blocked!
        self.linear_state.add_issue(
            "TST-GA5",
            "Git config remains immutable",
            "## Authorized Gate/Test Changes\n- gate: .gitattributes\n",
            labels=["agent-ready"],
        )
        res_cfg = self._run_cli(
            ["run", "TST-GA5", "--repo", str(self.repo), "--allow-gate-edits", "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "tamper_git_config"},
        )
        self.assertNotEqual(res_cfg.returncode, 0)
        self.assertIn("Unauthorized git config tampering", res_cfg.stderr)
        _git(self.repo, "config", "--unset", "core.hooksPath", check=False)

    def test_e2e_resume_linear_state_synced_respects_closed_pr_and_human_todo_and_cleanup_branch_owner(self) -> None:
        """Respect human PR, Linear and branch changes during recovery and cleanup."""
        # 1. Stage-aware resume at linear_state_synced MUST NOT recreate a human-closed PR or overwrite human Todo!
        iss = self.linear_state.add_issue(
            "TST-SYNC",
            "Stage aware resume",
            "## What to Build\nAdd mul\n\n## Testing Seam & Acceptance Criteria\n- [ ] ok\n",
            labels=["agent-ready"],
        )
        self.linear_state.fail_next_comment_create = True
        res1 = self._run_cli(["run", "TST-SYNC", "--repo", str(self.repo), "--skip-baseline"])
        self.assertEqual(res1.returncode, 1)
        out1 = json.loads(res1.stdout)
        self.assertEqual(out1["stage"], "linear_state_synced")

        # Close PR #1 -> resume MUST refuse without creating PR #2!
        prs = json.loads(self.gh_state_file.read_text())
        prs[0]["state"] = "CLOSED"
        self.gh_state_file.write_text(json.dumps(prs))
        res_closed = self._run_cli(["resume", "TST-SYNC", "--repo", str(self.repo)])
        self.assertEqual(res_closed.returncode, 2)
        self.assertEqual(len(json.loads(self.gh_state_file.read_text())), 1, "resume MUST NOT recreate human-closed PR!")

        # Reopen PR #1, move Linear back to Todo -> resume MUST refuse without forcing back to In Review!
        prs[0]["state"] = "OPEN"
        self.gh_state_file.write_text(json.dumps(prs))
        todo_st = next(s for s in self.linear_state.states if s["name"] == "Todo")
        iss["state"] = todo_st
        res_todo = self._run_cli(["resume", "TST-SYNC", "--repo", str(self.repo)])
        self.assertEqual(res_todo.returncode, 2)
        self.assertEqual(iss["state"]["name"], "Todo")

        # Restore In Review -> resume finishes ONLY Step 4 comment with ZERO `pr edit` calls!
        rev_st = next(s for s in self.linear_state.states if s["name"] == "In Review")
        iss["state"] = rev_st
        gh_before = self.gh_calls_log.read_text(encoding="utf-8")
        res_ok = self._run_cli(["resume", "TST-SYNC", "--repo", str(self.repo)])
        self.assertEqual(res_ok.returncode, 0, res_ok.stderr + res_ok.stdout)
        self.assertNotIn("pr edit", self.gh_calls_log.read_text(encoding="utf-8")[len(gh_before):])

        # 2. Cleanup --all MUST NOT delete worktree switched by human to a personal branch!
        wt = self.cache_dir / "worktrees" / ad.repo_cache_key(self.repo) / "TST-SYNC"
        _git(wt, "checkout", "-b", "personal/wip")
        clean_pers = self._run_cli(["cleanup", "--all", "--repo", str(self.repo)])
        self.assertEqual(clean_pers.returncode, 0)
        clean_pers_out = json.loads(clean_pers.stdout)
        self.assertEqual(clean_pers_out["removed"], [])
        self.assertTrue(any("branch ownership mismatch" in s for s in clean_pers_out["skipped"]), clean_pers_out)
        self.assertTrue(wt.exists())

        # 3. worker_blocked with uncommitted dirty file stashes cleanly so rerun succeeds WITHOUT --fresh, AND rollback adds label BEFORE state!
        iss_blk = self.linear_state.add_issue(
            "TST-BLK",
            "Blocked then rerun",
            "## What to Build\nAdd mul\n\n## Testing Seam & Acceptance Criteria\n- [ ] ok\n",
            labels=["agent-ready"],
        )
        mut_before = len(self.linear_state.mutation_log)
        res_blk = self._run_cli(
            ["run", "TST-BLK", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "blocked_dirty"},
        )
        self.assertEqual(res_blk.returncode, 1)
        self.assertEqual(json.loads(res_blk.stdout)["status"], "worker_blocked")
        # Verify rollback added `dispatch:failed` BEFORE reverting state to `Todo`!
        new_muts = self.linear_state.mutation_log[mut_before:]
        lbl_idx = next(i for i, (k, d) in enumerate(new_muts) if k == "addLabels" and "dispatch:failed" in d["labels"])
        todo_idx = next(i for i, (k, d) in enumerate(new_muts) if k == "state" and d["state"] == "Todo")
        self.assertLess(lbl_idx, todo_idx, f"dispatch:failed MUST be added BEFORE reverting to Todo! log={new_muts}")

        # Now rerun TST-BLK cleanly WITHOUT --fresh -> MUST succeed because blocked work was stashed!
        iss_blk["_labels"] = [l for l in iss_blk["_labels"] if l["name"] != "dispatch:failed"]
        res_rerun = self._run_cli(["run", "TST-BLK", "--repo", str(self.repo), "--skip-baseline"])
        self.assertEqual(res_rerun.returncode, 0, res_rerun.stderr + res_rerun.stdout)
        self.assertEqual(json.loads(res_rerun.stdout)["status"], "passed")

    def test_e2e_no_push_does_not_mutate_linear_and_crash_preserves_session_trace(self) -> None:
        iss = self.linear_state.add_issue(
            "TST-30",
            "Local dry run",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        res = self._run_cli(["run", "TST-30", "--repo", str(self.repo), "--no-push", "--skip-baseline"])
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(json.loads(res.stdout)["status"], "passed_local")
        # Zero Linear mutations in --no-push mode!
        self.assertEqual(self.linear_state.mutation_log, [])
        self.assertEqual(iss["state"]["name"], "Todo")

        # Worker crash on TST-31 MUST persist session trace in sessions.jsonl AND revert Linear to Todo + dispatch:failed!
        iss2 = self.linear_state.add_issue(
            "TST-31",
            "Crash test",
            "## What to Build\nCrash\n\n## Testing Seam & Acceptance Criteria\n- [ ] works\n",
            labels=["agent-ready"],
        )
        res_crash = self._run_cli(
            ["run", "TST-31", "--repo", str(self.repo), "--skip-baseline"],
            extra_env={"FAKE_CODEX_MODE": "crash"},
        )
        self.assertNotEqual(res_crash.returncode, 0)
        self.assertEqual(iss2["state"]["name"], "Todo")
        self.assertIn("dispatch:failed", [l["name"] for l in iss2["_labels"]])
        # Verify sessions.jsonl captured the crashed codex session_id!
        rkey = ad.repo_cache_key(self.repo)
        latest = json.loads((self.cache_dir / "runs" / rkey / "TST-31" / "latest.json").read_text())
        sfile = Path(latest["run_dir"]) / "sessions.jsonl"
        self.assertTrue(sfile.is_file())
        self.assertIn("11111111-2222-3333-4444", sfile.read_text())

    def test_install_sh_preserves_unrelated_skills_and_backs_up_real_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Copy install source into temp dir so the test NEVER mutates permissions in the live checkout!
            src_copy = d / "workflow_src"
            shutil.copytree(WORKFLOW_ROOT, src_copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            ag_skills = d / "agent_skills"
            ag_plugins = d / "agy_plugins"
            cx_skills = d / "codex_skills"
            cx_agents = d / "codex" / "AGENTS.md"
            bin_dst = d / "bin" / "agent-dispatch"
            (ag_skills / "my-custom-skill").mkdir(parents=True)
            (ag_skills / "my-custom-skill" / "SKILL.md").write_text("keep me", encoding="utf-8")
            (ag_skills / "my-grill").symlink_to(src_copy / "skills" / "grill")
            (ag_plugins / "unrelated-plugin").mkdir(parents=True)
            (ag_plugins / "unrelated-plugin" / "plugin.json").write_text('{"name":"unrelated"}\n', encoding="utf-8")
            (ag_plugins / "agent-workflow").mkdir(parents=True)
            (ag_plugins / "agent-workflow" / "plugin.json").write_text(
                '{"name":"agent-workflow","displayName":"Old Workflow Plugin"}\n', encoding="utf-8"
            )
            (ag_plugins / "agent-workflow" / "custom.txt").write_text("backup plugin", encoding="utf-8")
            cx_agents.parent.mkdir(parents=True)
            cx_agents.write_text("custom real AGENTS.md", encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "AGENT_SKILLS_DIR": str(ag_skills),
                    "AGY_PLUGINS_DIR": str(ag_plugins),
                    "CODEX_SKILLS_DIR": str(cx_skills),
                    "CODEX_AGENTS_MD": str(cx_agents),
                    "BIN_TARGET": str(bin_dst),
                }
            )
            env.pop("AGY_PLUGINS_BACKUP_DIR", None)
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            initial_links = {p: p.lstat().st_ino for p in ag_skills.iterdir()}
            plugin_inode = (ag_plugins / "agent-workflow").lstat().st_ino
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            self.assertEqual(initial_links, {p: p.lstat().st_ino for p in ag_skills.iterdir()})
            self.assertEqual((ag_plugins / "agent-workflow").lstat().st_ino, plugin_inode)
            self.assertEqual((ag_skills / "my-grill").resolve(), src_copy / "skills" / "grill")
            # Unrelated skill and plugin preserved!
            self.assertTrue((ag_skills / "my-custom-skill" / "SKILL.md").is_file())
            self.assertTrue((ag_plugins / "unrelated-plugin" / "plugin.json").is_file())
            # Global Antigravity rules plugin linked and conflicting plugin moved OUTSIDE auto-discovery dir!
            self.assertEqual((ag_plugins / "agent-workflow").resolve(), src_copy / "agy" / "plugin")
            self.assertTrue((ag_plugins / "agent-workflow" / "rules" / "AGENTS.md").is_file())
            self.assertEqual(list(ag_plugins.glob("*.bak.*")), [], "Plugin backup must NOT remain inside AGY_PLUGINS_DIR!")
            plugin_bak_root = d / "plugins.bak"
            plugin_backups = list(plugin_bak_root.glob("agent-workflow.bak.*"))
            self.assertEqual(len(plugin_backups), 1)
            self.assertEqual(
                json.loads((plugin_backups[0] / "plugin.json").read_text(encoding="utf-8"))["displayName"],
                "Old Workflow Plugin",
            )
            self.assertEqual((plugin_backups[0] / "custom.txt").read_text(encoding="utf-8"), "backup plugin")
            # Conflicting symlink to an old checkout is also moved out of AGY_PLUGINS_DIR into plugins.bak!
            old_checkout = d / "old_checkout_plugin"
            old_checkout.mkdir()
            (old_checkout / "plugin.json").write_text('{"name":"agent-workflow","displayName":"Relocated"}\n', encoding="utf-8")
            (ag_plugins / "agent-workflow").unlink()
            (ag_plugins / "agent-workflow").symlink_to(old_checkout)
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            self.assertEqual(list(ag_plugins.glob("*.bak.*")), [])
            self.assertEqual((ag_plugins / "agent-workflow").resolve(), src_copy / "agy" / "plugin")
            self.assertEqual(len(list(plugin_bak_root.glob("agent-workflow.bak.*"))), 2)
            # Setting AGY_PLUGINS_BACKUP_DIR inside AGY_PLUGINS_DIR fails closed!
            bad_env = dict(env, AGY_PLUGINS_BACKUP_DIR=str(ag_plugins / "nested-bak"))
            bad_res = subprocess.run([str(src_copy / "install.sh")], env=bad_env, capture_output=True, text=True)
            self.assertNotEqual(bad_res.returncode, 0)
            # Real AGENTS.md backed up, not destroyed!
            backups = list(cx_agents.parent.glob("AGENTS.md.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "custom real AGENTS.md")
            # Orchestrator skills NOT symlinked into codex skills!
            self.assertFalse((cx_skills / "dispatch").exists())
            self.assertFalse((cx_skills / "slice").exists())
            self.assertTrue((cx_skills / "grill").is_symlink())
            self.assertTrue((ag_skills / "dispatch").is_symlink())

    def test_installer_resolves_script_and_registry_aliases(self) -> None:
        source = self.root / "source"
        shutil.copytree(WORKFLOW_ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        script = self.root / "relink"
        script.symlink_to(source / "install.sh")
        agent_skills = self.root / "agent-skills"
        agent_skills.symlink_to(source / "skills")
        agy_plugins = self.root / "agy-plugins"
        worker_skills = self.root / "worker-skills"
        worker_skills.mkdir()
        relative = worker_skills / "research"
        relative.symlink_to("../source/skills/research")
        inode = relative.lstat().st_ino
        retired = worker_skills / "dispatch"
        retired.symlink_to("../source/skills/dispatch")
        env = dict(os.environ, AGENT_SKILLS_DIR=str(agent_skills), AGY_PLUGINS_DIR=str(agy_plugins),
                   CODEX_SKILLS_DIR=str(worker_skills), CODEX_AGENTS_MD=str(self.root / "AGENTS.md"),
                   BIN_TARGET=str(self.root / "agent-dispatch"))
        subprocess.run([str(script)], env=env, check=True, capture_output=True)
        self.assertTrue((agent_skills / "dispatch/SKILL.md").is_file())
        self.assertFalse((source / "skills/dispatch").is_symlink())
        self.assertFalse(retired.is_symlink())
        self.assertEqual(relative.lstat().st_ino, inode)
        self.assertEqual((agy_plugins / "agent-workflow").resolve(), source / "agy/plugin")
        self.assertEqual((self.root / "agent-dispatch").resolve(), source / "bin/agent-dispatch")
        self.assertFalse(list(source.rglob("*.bak.*")))

        # Both rosters cannot be installed into the same physical directory.
        env["CODEX_SKILLS_DIR"] = str(source / "skills")
        conflict = subprocess.run([str(script)], env=env, capture_output=True)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertTrue((agent_skills / "dispatch/SKILL.md").is_file())

    def test_cleanup_reclaims_bootstrapped_baseline_with_untracked_files_and_protects_tracked_edits(self) -> None:
        base = _git(self.repo, "rev-parse", "HEAD")
        rkey = ad.repo_cache_key(self.repo)
        wt_root = self.cache_dir / "worktrees" / rkey
        owner_root = self.cache_dir / "baseline-owners" / rkey
        wt_root.mkdir(parents=True, exist_ok=True)
        owner_root.mkdir(parents=True, exist_ok=True)

        # 1. Abandoned baseline with untracked bootstrap artifacts (e.g., node_modules) -> reclaimed by plain cleanup!
        wt_untracked = wt_root / ".baseline-untracked-test"
        _git(self.repo, "worktree", "add", "--detach", str(wt_untracked), base)
        (wt_untracked / "node_modules").mkdir()
        (wt_untracked / "node_modules" / "pkg.js").write_text("module.exports = 1;\n", encoding="utf-8")
        (owner_root / wt_untracked.name).write_text(json.dumps({"key": "a" * 64, "base_sha": base}), encoding="utf-8")

        # 2. Abandoned baseline with modified tracked file -> preserved by plain cleanup, reclaimed by --all!
        wt_tracked = wt_root / ".baseline-tracked-test"
        _git(self.repo, "worktree", "add", "--detach", str(wt_tracked), base)
        (wt_tracked / "app.py").write_text("# human edit\n", encoding="utf-8")
        (owner_root / wt_tracked.name).write_text(json.dumps({"key": "b" * 64, "base_sha": base}), encoding="utf-8")

        out1 = self._run_cli(["cleanup"])
        self.assertEqual(out1.returncode, 0, out1.stderr)
        parsed1 = json.loads(out1.stdout)
        self.assertIn(wt_untracked.name, parsed1["removed"])
        self.assertFalse(wt_untracked.exists())
        self.assertTrue(wt_tracked.exists())
        self.assertTrue(any(wt_tracked.name in s for s in parsed1["skipped"]))

        out2 = self._run_cli(["cleanup", "--all"])
        self.assertEqual(out2.returncode, 0, out2.stderr)
        self.assertIn(wt_tracked.name, json.loads(out2.stdout)["removed"])
        self.assertFalse(wt_tracked.exists())

    def test_e2e_frontier_sigterm_cancels_queued_ticket_and_suppresses_bogus_json(self) -> None:
        t1 = self.linear_state.add_issue(
            "TST-S1", "Basic validation alpha", "## Touches\n- alpha\n", labels=["agent-ready"]
        )
        t2 = self.linear_state.add_issue(
            "TST-S2", "Basic validation beta", "## Touches\n- beta\n", labels=["agent-ready"]
        )
        entered = self.root / "s1_entered"
        _write_exec(
            self.fake_bin / "codex",
            f"""#!/usr/bin/env python3
import pathlib, time
pathlib.Path({str(entered)!r}).touch()
time.sleep(30)
""",
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.fake_bin}:{env.get('PATH', '')}",
                "AGENT_DISPATCH_LINEAR_URL": self.linear_url,
                "AGENT_DISPATCH_CACHE_ROOT": str(self.cache_dir),
                "LINEAR_API_KEY": "lin_api_fake_e2e_key_123456",
                "AGENT_DISPATCH_FORCE_GH": "1",
                "AGENT_DISPATCH_PR_POLL_SLEEP": "0.01",
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "-B", str(BIN_PATH), "frontier", "--repo", str(self.repo), "--max-parallel", "1", "--skip-baseline"],
            cwd=str(self.repo),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while not entered.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(entered.exists(), "First frontier ticket never started worker")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        self.assertNotEqual(proc.returncode, 0, f"Expected non-zero signal exit; stdout={out} stderr={err}")
        self.assertEqual(out.strip(), "", f"SIGTERM during frontier must not print completion JSON: {out}")
        # First ticket rolled back cleanly with dispatch:failed; second ticket was cancelled before claiming!
        self.assertEqual(t1["state"]["name"], "Todo")
        self.assertIn("dispatch:failed", [l["name"] for l in t1["_labels"]])
        self.assertEqual(t2["state"]["name"], "Todo")
        self.assertNotIn("dispatch:failed", [l["name"] for l in t2["_labels"]])


# ---------------------------------------------------------------------------
# Opt-in Live Read-Only Linear GraphQL Contract Tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.environ.get("RUN_LIVE_LINEAR_TESTS") == "1", "Set RUN_LIVE_LINEAR_TESTS=1 for live Linear checks")
class TestLiveLinearReadOnlyContract(unittest.TestCase):
    def setUp(self) -> None:
        # Pin the authorized origin and reject any accidental mutation before creating a request.
        self.client = ad.LinearClient(ad.resolve_linear_auth(), url="https://api.linear.app/graphql")
        query = self.client.query
        def readonly(document, variables=None, **opts):
            self.assertTrue(document.lstrip().startswith("query"))
            self.assertNotRegex(document, r"\bmutation\s*[{(]")
            return query(document, variables, **opts)
        self.enterContext(mock.patch.object(self.client, "query", side_effect=readonly))

    def test_atomic_label_contract(self) -> None:
        schema = self.client.query('query { __type(name: "Mutation") { fields { name args { name } } } }')
        fields = {f["name"]: {arg["name"] for arg in f["args"]} for f in schema["__type"]["fields"]}
        for operation in ("issueAddLabel", "issueRemoveLabel"):
            self.assertEqual(fields[operation], {"id", "labelId"})

    def test_live_linear_project_string_query_and_pagination(self) -> None:
        teams, projects = self.client.list_teams_and_projects()
        self.assertTrue(len(teams) >= 1)
        self.assertTrue(len(projects) >= 1)
        issues = self.client.list_project_issues(projects[0]["id"], state_type="unstarted")
        self.assertIsInstance(issues, list)

    def test_explicit_project_typo_fails_fast_with_specific_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "Explicit Linear project"):
                ad.resolve_project(self.client, Path(td), explicit="definitely-nonexistent-project-xyz")


if __name__ == "__main__":
    unittest.main()
