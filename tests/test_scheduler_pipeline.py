"""Contract tests for the ticket pipeline and Frontier Wave scheduler."""

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from agent_dispatch import brief, config, git_ops, harness, linear, pipeline, process, publish, recovery, review, scheduler

from tests.helpers import FakeLinearState, IsolatedDispatchCase, _git, start_fake_linear


class TestRecovery(IsolatedDispatchCase):
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
                recovery.stash_dirty_worktree_for_safe_rerun(repo, "halt")
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
            recovery.stash_dirty_worktree_for_safe_rerun(repo, "halt")
            self.assertEqual(_git(repo, "stash", "list"), "")
            self.assertEqual(_git(repo, "ls-files", "--stage"), index_before)
            self.assertEqual(tracked.read_text(), "base")
            _git(repo, "reset", "--", "tracked.txt")
            (repo / "safe.txt").write_text("save me")
            process.request_shutdown(signal.SIGHUP)
            try:
                recovery.stash_dirty_worktree_for_safe_rerun(repo, "halt")
            finally:
                process.reset_shutdown()
            self.assertIn("halt", _git(repo, "stash", "list"))
            self.assertEqual(_git(repo, "show", "stash^3:safe.txt"), "save me")


class TestTicketPipeline(IsolatedDispatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(config, "MCP_CONFIG_PATH", self.sandbox / "unused-mcp.json"))

    def test_worker_rechecks_immutable_brief_before_timeout_rework(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "base")
            linear_state = FakeLinearState("test")
            linear_state.add_issue("TST-1", "Implement task", "## What to Build\nFeature")
            srv, url = start_fake_linear(linear_state)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)

            def change_brief_and_timeout(spec: harness.HarnessSpec, prompt: str, **kwargs: Any) -> None:
                brief_path = kwargs["run_dir"] / "brief.md"
                self.assertIn(str(brief_path), prompt)
                brief_path.write_text("changed during first worker")
                raise subprocess.TimeoutExpired("fake worker", 1)

            with mock.patch.object(pipeline, "invoke_harness", side_effect=change_brief_and_timeout) as invoke:
                with self.assertRaisesRegex(RuntimeError, "integrity violation.*brief.md"):
                    pipeline.execute_ticket(
                        linear.LinearClient("synthetic", url=url), repo, None, "TST-1",
                        harness.parse_harness_spec("codex"), harness.parse_harness_spec("agy"),
                        verify_cmd_override="true", setup_cmd_override=None, max_rework=1,
                        verify_timeout=5, setup_timeout=5, no_push=True, allow_no_verify=False,
                        allow_gate_edits=False, allow_test_edits=False, allow_dirty_docs=True,
                        skip_baseline=True, skip_project_check=True, force_rerun=False, fresh=False,
                    )
                self.assertEqual(invoke.call_count, 1)

    def test_publish_signal_preserves_resumable_stage(self) -> None:
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

            ls_state = FakeLinearState(project_name="repo")
            iss = ls_state.add_issue("TST-SIG", "Signal during publish", "## What to Build\nx\n", labels=["agent-ready"])
            srv, url = start_fake_linear(ls_state)
            try:
                client = linear.LinearClient("synthetic", url=url)
                cache_dir = d / "sig-cache"

                def fake_harness_invoke(spec: Any, prompt: str, **kw: Any) -> dict[str, Any]:
                    if not kw["read_only"]:
                        (kw["worktree"] / "a.py").write_text("def x(): return 99\n", encoding="utf-8")
                        return {"status": "completed", "summary": "done", "tests_run": ["unittest"], "discovered_issues": [], "blockers": ""}
                    return {"axis": kw["expected_axis"], "verdict": "pass", "hard_findings": [], "suggestions": [], "test_integrity_violation": False, "summary": "ok"}

                def interrupt_git_push(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                    if cmd[0] == "git" and "push" in cmd:
                        process.request_shutdown(signal.SIGINT)
                        raise KeyboardInterrupt("Simulated SIGINT mid-push")
                    return process.run_cmd(cmd, **kwargs)

                try:
                    with (
                        mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(cache_dir)}),
                        mock.patch.object(pipeline, "invoke_harness", side_effect=fake_harness_invoke),
                        mock.patch.object(review, "invoke_harness", side_effect=fake_harness_invoke),
                        mock.patch.object(publish, "run_cmd", side_effect=interrupt_git_push),
                        self.assertRaises(KeyboardInterrupt),
                    ):
                        pipeline.execute_ticket(
                            client,
                            repo,
                            None,
                            "TST-SIG",
                            harness.parse_harness_spec("codex:sol"),
                            harness.parse_harness_spec("agy:neon"),
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
                rkey = git_ops.repo_cache_key(repo)
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


class TestFrontierWave(IsolatedDispatchCase):
    def test_touches_wave_selection_defers_overlapping_and_unscoped_issues(self) -> None:
        # Hierarchical Touches overlap: src/routes and src/routes/admin MUST NOT share a wave!
        iss_a = {"identifier": "TST-1", "description": "## Touches\n- src/routes\n"}
        iss_b = {"identifier": "TST-2", "description": "## Touches\n- src/auth\n"}
        iss_c = {"identifier": "TST-3", "description": "## Touches\n- src/routes/admin\n"}
        iss_d = {"identifier": "TST-4", "description": "## What to Build\nno touches declared"}
        wave, deferred = scheduler.select_non_overlapping_wave([iss_a, iss_b, iss_c, iss_d])
        self.assertEqual([i["identifier"] for i in wave], ["TST-1", "TST-2"])
        self.assertEqual([d["id"] for d in deferred], ["TST-3", "TST-4"])

    def test_wildcard_touches_reserve_all_matching_descendants(self) -> None:
        candidates = [{"identifier": "A", "description": "## Touches\n- src/*.py"},
                      {"identifier": "B", "description": "## Touches\n- src/core.py"}]
        selected, deferred = scheduler.select_non_overlapping_wave(candidates)
        self.assertEqual(selected, candidates[:1])
        self.assertEqual([entry["id"] for entry in deferred], ["B"])

    def test_numbered_touches_conflict_in_wave_and_locks(self) -> None:
        # Unordered list and numbered list for the same path MUST conflict in wave selection and local touch locks.
        iss_bullet = {"identifier": "TST-BUL", "description": "## Touches\n- src/auth\n"}
        iss_num = {"identifier": "TST-NUM", "description": "## Touches\n1. `src/auth`\n"}
        iss_paren = {"identifier": "TST-PAR", "description": "## Touches\n2) `src/auth/login`\n"}
        iss_other = {"identifier": "TST-OTH", "description": "## Touches\n10. `src/billing`\n"}
        wave, deferred = scheduler.select_non_overlapping_wave([iss_bullet, iss_num, iss_paren, iss_other])
        self.assertEqual([i["identifier"] for i in wave], ["TST-BUL", "TST-OTH"])
        self.assertEqual([d["id"] for d in deferred], ["TST-NUM", "TST-PAR"])

        t_bullet = brief.parse_touches(iss_bullet["description"])
        t_num = brief.parse_touches(iss_num["description"])
        with scheduler.reserve_touches("repo-touches-num-test", t_bullet) as locked1:
            self.assertTrue(locked1)
            with scheduler.reserve_touches("repo-touches-num-test", t_num) as locked2:
                self.assertFalse(locked2)
