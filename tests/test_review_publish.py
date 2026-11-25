"""Contract tests for Two-Axis Review and Draft PR Delivery."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_dispatch import brief, gates, git_ops, harness, linear, process, publish, review
from tests.helpers import FakeLinearState, IsolatedDispatchCase, _git, _write_exec, start_fake_linear


class TestReview(IsolatedDispatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_bin = self.sandbox / "bin"
        self.fake_bin.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {
            "PATH": str(self.fake_bin) + os.pathsep + os.environ["PATH"],
            "AGENT_DISPATCH_REVIEW_RETRY_SLEEP": "0",
        }))
        self.reviewer_calls = self.sandbox / "reviewer-calls"

    def _install_reviewer(self, before_verdict: str = "") -> None:
        _write_exec(
            self.fake_bin / "agy",
            f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

calls_file = Path({str(self.reviewer_calls)!r})
schema = Path(sys.argv[sys.argv.index("--json-schema") + 1])
axis = schema.stem.removeprefix("review-").removesuffix("-schema")
with calls_file.open("a", encoding="utf-8") as calls:
    calls.write(axis + "\\n")
{before_verdict}
print(json.dumps({{"status": "SUCCESS", "structured_output": {{
    "axis": axis, "verdict": "pass", "hard_findings": [], "suggestions": [],
    "test_integrity_violation": False, "summary": "ok",
}}}}))
""",
        )

    def _install_reviewer_checking_redaction(self, patch_path: Path, token: str) -> None:
        self._install_reviewer(f"""
patch = Path({str(patch_path)!r}).read_text().replace(chr(0), "")
assert {token!r} not in patch
assert "<REDACTED_SECRET>" in patch
""")

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
            self._install_reviewer_checking_redaction(run_dir / "diff-r1.patch", token)
            result = review.run_two_axis_review(
                harness.parse_harness_spec("agy"), repo, run_dir, introduced, cleaned,
                None, None, run_dir, {}, 1, block_added_secrets=True,
            )
            self.assertTrue(result["passed"])
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
            self._install_reviewer(f"""
Path({str(run_dir / 'diff-r1.patch')!r}).write_text("modified after first review")
print("incomplete verdict")
sys.exit(0)
""")
            with self.assertRaisesRegex(RuntimeError, "integrity violation.*diff-r1"):
                review.run_two_axis_review(harness.parse_harness_spec("agy"), d, run_dir, head, head,
                                       None, None, run_dir / "standards", {}, 1)
            self.assertEqual(self.reviewer_calls.read_text().splitlines(), ["standards"])

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
                    self._install_reviewer_checking_redaction(run_dir / "diff-r1.patch", token)
                    result = review.run_two_axis_review(
                        harness.parse_harness_spec("agy"), repo, run_dir, introduced,
                        _git(repo, "rev-parse", "HEAD"), None, None, run_dir, {}, 1,
                    )
                    self.assertTrue(result["passed"])
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
                        self._install_reviewer_checking_redaction(run_dir / "diff-r1.patch", token)
                        result = review.run_two_axis_review(
                            harness.parse_harness_spec("agy"), repo, run_dir, "HEAD^",
                            _git(repo, "rev-parse", "HEAD"), None, None, run_dir, {}, 1,
                            block_added_secrets=True,
                        )
                        self.assertTrue(result["passed"])
                        patch = (run_dir / "diff-r1.patch").read_text()
                        self.assertNotIn(token, patch.replace("\0", ""))
                        self.assertIn("<REDACTED_SECRET>", patch)

    def test_review_retries_json_and_checks_standards_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "CODING_STANDARDS.md").write_text("# Standards\n", encoding="utf-8")
            (repo / "a.py").write_text("def x(): return 1\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
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
            _, std_hashes = brief.snapshot_baseline_standards(repo, base_sha, std_dir)

            self._install_reviewer("""
if len(calls_file.read_text().splitlines()) == 1:
    print("malformed JSON on first try")
    sys.exit(0)
""")
            result = review.run_two_axis_review(
                harness.parse_harness_spec("agy:neon"), repo, run_dir, base_sha, cand_sha,
                None, None, std_dir, std_hashes, 1,
            )
            self.assertTrue(result["passed"])
            self.assertEqual(self.reviewer_calls.read_text().splitlines(), ["standards", "standards"])

            self._install_reviewer(f"""
Path({str(std_dir / 'CODING_STANDARDS.md')!r}).write_text("# TAMPERED")
""")
            with self.assertRaisesRegex(RuntimeError, "integrity violation.*CODING_STANDARDS"):
                review.run_two_axis_review(
                    harness.parse_harness_spec("agy:neon"), repo, run_dir, base_sha, cand_sha,
                    None, None, std_dir, std_hashes, 2,
                )

    def test_review_blocks_added_secrets_redacts_removed_secrets_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Standalone review blocks added secret BEFORE calling reviewer harness (0 LLM calls!)
            s_repo = d / "sec_rev"
            s_repo.mkdir()
            _git(s_repo, "init", "-b", "main")
            _git(s_repo, "config", "user.email", "t@example.com")
            _git(s_repo, "config", "user.name", "T")
            old_token = "lin_api_" + "SYNTHETIC" * 3
            (s_repo / "a.py").write_text(f"OLD = {old_token!r}\n", encoding="utf-8")
            _git(s_repo, "add", "-A")
            _git(s_repo, "commit", "-m", "init")
            b_sha = _git(s_repo, "rev-parse", "HEAD")
            new_token_prefix = "github_pat_SYNTHETIC"
            new_token = new_token_prefix + "SYNTHETIC" * 2
            (s_repo / "a.py").write_text(f"NEW = {new_token!r}\n", encoding="utf-8")
            _git(s_repo, "add", "-A")
            _git(s_repo, "commit", "-m", "add secret")
            c_sha = _git(s_repo, "rev-parse", "HEAD")
            self._install_reviewer()
            with self.assertRaisesRegex(RuntimeError, "added secrets"):
                review.run_two_axis_review(
                    harness.parse_harness_spec("agy:neon"), s_repo, d / "r_sec", b_sha, c_sha,
                    None, None, d / "std", {}, 1, block_added_secrets=True,
                )
            self.assertFalse(self.reviewer_calls.exists(), "Reviewer harness MUST NOT be invoked when added secrets detected!")

            # Removing old secret redacts deleted secret line from diff-r1.patch before reviewer sees it!
            (s_repo / "a.py").write_text("CLEAN = 'ok'\n", encoding="utf-8")
            _git(s_repo, "add", "-A")
            _git(s_repo, "commit", "-m", "remove secret")
            clean_sha = _git(s_repo, "rev-parse", "HEAD")
            res_cln = review.run_two_axis_review(
                harness.parse_harness_spec("agy:neon"), s_repo, d / "r_cln", c_sha, clean_sha,
                None, None, d / "std2", {}, 1, block_added_secrets=True,
            )
            self.assertTrue(res_cln["diff_redacted_for_review"])
            self.assertNotIn(new_token_prefix, (d / "r_cln" / "diff-r1.patch").read_text(encoding="utf-8"))

            # Reviewer transient RuntimeError auto-retries once!
            self.reviewer_calls.unlink()
            self._install_reviewer("""
if len(calls_file.read_text().splitlines()) == 1:
    print("transient 503", file=sys.stderr)
    sys.exit(1)
""")
            r_ok = review.run_two_axis_review(
                harness.parse_harness_spec("agy:neon"), s_repo, d / "r_flk", b_sha, clean_sha,
                None, None, d / "std3", {}, 1,
            )
            self.assertTrue(r_ok["passed"])
            self.assertEqual(self.reviewer_calls.read_text().splitlines(), ["standards", "standards"])

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
                "venv_cacert.pem": gates.file_identity(repo / "venv_cacert.pem"),
                "cache.tmp": gates.file_identity(repo / "cache.tmp"),
            }
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            # Simulate verify/worker deleting cache.tmp before review while venv_cacert.pem remains
            (repo / "cache.tmp").unlink()
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "candidate")
            cand = _git(repo, "rev-parse", "HEAD")

            self._install_reviewer()
            rev = review.run_two_axis_review(
                harness.parse_harness_spec("agy:neon"), repo, root / "r1", base, cand,
                None, None, root / "std", {}, 1, setup_files=setup_files,
            )
            self.assertTrue(rev["passed"])

            self._install_reviewer("""
Path("venv_cacert.pem").write_text("tampered")
""")
            with self.assertRaisesRegex(RuntimeError, "Reviewer mutated worktree"):
                review.run_two_axis_review(
                    harness.parse_harness_spec("agy:neon"), repo, root / "r2", base, cand,
                    None, None, root / "std", {}, 1, setup_files=setup_files,
                )


class TestPublish(IsolatedDispatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_bin = self.sandbox / "bin"
        self.fake_bin.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"PATH": str(self.fake_bin) + os.pathsep + os.environ["PATH"]}))

    def _install_gh(self, responses: dict[tuple[str, ...], str]) -> None:
        _write_exec(
            self.fake_bin / "gh",
            f"""#!/usr/bin/env python3
import sys

responses = {responses!r}
command = tuple(sys.argv[1:2] if sys.argv[1] == "api" else sys.argv[1:3])
print(responses[command])
""",
        )

    def test_pr_markdown_rendering(self) -> None:
        token_prefix = "lin_api_SYNTHETIC"
        token = token_prefix + "SYNTHETIC" * 2
        body = publish.build_pr_body(
            {"identifier": "K1N-99", "title": "Add auth", "url": "https://linear.app/k1n/issue/K1N-99"},
            "a" * 40,
            harness.parse_harness_spec("codex:sol"),
            harness.parse_harness_spec("agy:neon"),
            f"Implemented auth with {token}",
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
        self.assertNotIn(token_prefix, body)
        self.assertIn("<REDACTED_SECRET>", body)

    def test_pr_feedback_trust_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 1. PR feedback: empty authorAssociation & NONE MUST be rejected; OWNER accepted; paginated --slurp flattened!
            prs = [
                {"number": 7, "url": "u", "isDraft": True, "isCrossRepository": False, "headRefName": "feat/x"},
                {"number": 8, "isCrossRepository": True, "headRefName": "feat/x"},
            ]
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
            pages = [
                [{"author_association": "", "body": "bad inline", "path": "a.py", "line": 1, "user": {"login": "u"}}],
                [{"author_association": "MEMBER", "body": "good inline", "path": "b.py", "line": 9, "user": {"login": "m"}}],
            ]
            self._install_gh({
                ("pr", "list"): json.dumps(prs),
                ("pr", "view"): json.dumps(payload),
                ("api",): json.dumps(pages),
            })
            issue_pr = publish.fetch_open_pr_for_branch(d, "test/repo", "feat/x")
            self.assertEqual(issue_pr["number"], 7)
            fb = publish.fetch_open_pr_feedback(d, "test/repo", issue_pr["number"]) or ""
            self.assertIn("fix null check", fb)
            self.assertIn("good inline", fb)
            self.assertNotIn("untrusted empty", fb)
            self.assertNotIn("untrusted none", fb)
            self.assertNotIn("bad inline", fb)

    def test_fetch_open_pr_rejects_multiple_open_prs_for_same_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # Multi-PR on same head fails closed BEFORE git push!
            two_prs = [
                {"number": 1, "url": "u1", "state": "OPEN", "isDraft": True, "baseRefName": "main", "headRefOid": "a" * 40},
                {"number": 2, "url": "u2", "state": "OPEN", "isDraft": False, "baseRefName": "release", "headRefOid": "a" * 40},
            ]
            for pr in two_prs:
                pr.update(isCrossRepository=False, headRefName="feat/x")
            self._install_gh({("pr", "list"): json.dumps(two_prs)})
            with self.assertRaisesRegex(RuntimeError, "Multiple open PRs share head branch"):
                publish.fetch_open_pr_for_branch(repo, "org/repo", "feat/x")

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
                client = linear.LinearClient("fake-linear", url=url)
                run_dir = d / "run"
                run_dir.mkdir()
                latest_f = d / "latest.json"
                state = {
                    "run_id": "r1",
                    "publish_destination": process.sha256_text(str(bare)),
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
                non_draft = {
                    "state": "OPEN", "number": 5, "url": "u", "isDraft": False,
                    "baseRefName": "main", "headRefOid": "0" * 40,
                    "isCrossRepository": False, "headRefName": "feat/tst-p",
                }
                self._install_gh({("pr", "list"): json.dumps([non_draft])})
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_FORCE_GH": "1", "AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                    with self.assertRaisesRegex(RuntimeError, "not a Draft PR"):
                        publish.reconcile_publish(client, iss, repo, run_dir, latest_f, dict(state))
                # A legacy GitHub checkpoint still has the identity of the original owner/repo.
                _git(repo, "remote", "set-url", "origin", "https://github.com/test/repo.git")
                legacy = dict(state, repo_identity=git_ops.canonical_repo_identity(repo))
                del legacy["publish_destination"]
                with self.assertRaisesRegex(RuntimeError, "not a Draft PR"):
                    publish.reconcile_publish(client, iss, repo, run_dir, latest_f, legacy)
                _git(repo, "remote", "set-url", "origin", str(bare))
                # Remote branch MUST NOT have been pushed!
                self.assertEqual(git_ops.get_remote_branch_sha(repo, "feat/tst-p"), "")

                # 2. Missing read-back after gh pr create MUST stay at stage='pushed' (NEVER 'completed')!
                st2 = dict(state)
                self._install_gh({("pr", "list"): "[]", ("pr", "create"): "https://github.com/t/r/pull/9"})
                with mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_DISPATCH_FORCE_GH": "1",
                        "AGENT_DISPATCH_CACHE_ROOT": str(d / "cache"),
                        "AGENT_DISPATCH_PR_POLL_ATTEMPTS": "1",
                        "AGENT_DISPATCH_PR_POLL_SLEEP": "0",
                    },
                ):
                    with self.assertRaisesRegex(RuntimeError, "no open PR"):
                        publish.reconcile_publish(client, iss, repo, run_dir, latest_f, st2)
                self.assertEqual(st2["stage"], "pushed")
                self.assertTrue(st2["pushed"])
            finally:
                srv.shutdown()
                srv.server_close()
