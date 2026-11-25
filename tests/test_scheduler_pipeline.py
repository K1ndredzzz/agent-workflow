"""Contract tests for the ticket pipeline and Frontier Wave scheduler."""

import json
import os
import signal
import subprocess
import tempfile
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from agent_dispatch import (
    brief, cli, config, git_ops, harness, linear, pipeline, preflight, process, publish, recovery, review, scheduler,
)

from tests.helpers import FakeLinearState, IsolatedDispatchCase, _git, _write_exec, start_fake_linear


class TestCLI(IsolatedDispatchCase):
    def setUp(self) -> None:
        super().setUp()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))

    def test_rework_rejects_values_outside_the_inclusive_range(self) -> None:
        for value in ("-1", "6"):
            with self.subTest(value=value), redirect_stderr(StringIO()) as stderr:
                with self.assertRaises(SystemExit) as exit_info:
                    cli.main(["run", "TST-1", "--max-rework", value])
                self.assertEqual(exit_info.exception.code, 2)
                self.assertIn(f"--max-rework must be between 0 and 5 (got {value})", stderr.getvalue())

    def test_positive_integer_flags_reject_zero_and_negative_values(self) -> None:
        cases = (
            (["frontier"], "--max-parallel", "0"),
            (["spec", "TST-1"], "--limit", "-1"),
            (["review"], "--review-timeout", "0"),
            (["run", "TST-1"], "--verify-timeout", "-1"),
            (["run", "TST-1"], "--setup-timeout", "0"),
            (["run", "TST-1"], "--worker-timeout", "-1"),
        )
        for command, flag, value in cases:
            with self.subTest(command=command, flag=flag), redirect_stderr(StringIO()) as stderr:
                with self.assertRaises(SystemExit) as exit_info:
                    cli.main([*command, flag, value])
                self.assertEqual(exit_info.exception.code, 2)
                self.assertIn(f"argument {flag}: must be a positive integer (got {value})", stderr.getvalue())

    def test_rework_accepts_both_endpoints_and_positive_timeout(self) -> None:
        for value in ("0", "5"):
            with self.subTest(value=value), redirect_stderr(StringIO()) as stderr:
                status = cli.main(["run", "TST-1", "--repo", str(self.sandbox),
                                   "--max-rework", value, "--verify-timeout", "1"])
                self.assertEqual(status, 2)
                error = json.loads(stderr.getvalue())
                self.assertEqual(error["status"], "error")
                self.assertIn("Not inside a git repository", error["error"])

    def test_routes_each_subcommand(self) -> None:
        repo = self.sandbox / "demo-repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-m", "base")
        fake_bin = self.sandbox / "bin"
        fake_bin.mkdir()
        _write_exec(fake_bin / "gh", "#!/bin/sh\nexit 1\n")
        state = FakeLinearState()
        state.add_issue("TST-1", "Example ticket", "")
        server, url = start_fake_linear(state)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.enterContext(mock.patch.dict(os.environ, {
            "AGENT_DISPATCH_LINEAR_URL": url,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }))

        cases = (
            (["run", "TST-1", "--worker", "unknown"], 2, "error", "Unsupported harness 'unknown'"),
            (["resume", "TST-1"], 2, "error", "No checkpoint found"),
            (["frontier"], 0, "status", "empty_frontier"),
            (["spec", "TST-1"], 2, "error", "TST-1 has no child issues."),
            (["review", "--base", "main"], 2, "error", "Empty diff between"),
            (["status"], 0, "linear_project", "demo-repo"),
            (["doctor"], 1, "ok", False),
            (["cleanup"], 0, "removed", []),
        )
        for command, status, key, expected in cases:
            with self.subTest(command=command), redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                self.assertEqual(cli.main([*command, "--repo", str(repo)]), status)
                if status == 2:
                    self.assertEqual(stdout.getvalue(), "")
                    data = json.loads(stderr.getvalue())
                    self.assertEqual(data["status"], "error")
                    self.assertIn(expected, data[key])
                else:
                    data = json.loads(stdout.getvalue())
                    self.assertEqual(data[key], expected)
                    if command == ["doctor"]:
                        self.assertEqual(data["checks"]["gh_auth"], {"ok": False})


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

    def test_preflight_saves_checkpoint_before_claim(self) -> None:
        remote = self.sandbox / "origin.git"
        repo = self.sandbox / "repo"
        _git(self.sandbox, "init", "--bare", "-b", "main", str(remote))
        _git(self.sandbox, "clone", str(remote), str(repo))
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-m", "base")
        _git(repo, "push", "-u", "origin", "main")
        state = FakeLinearState()
        state.add_issue("TST-1", "Implement task", "## What to Build\nFeature")
        server, url = start_fake_linear(state)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = linear.LinearClient("synthetic", url=url)
        options = preflight.RunOptions(
            verify_cmd_override="true", setup_cmd_override=None, max_rework=0,
            verify_timeout=5, setup_timeout=5, worker_timeout=5, review_timeout=5,
            no_push=False, allow_no_verify=False, allow_gate_edits=False, allow_test_edits=False,
            allow_dirty_docs=False, allow_branch_only=False, skip_baseline=True, skip_project_check=False,
            force_rerun=False, fresh=False, auto_claimed=False, reserved_touches=None,
        )
        with ExitStack() as cleanup:
            run = preflight.prepare_ticket(
                client, preflight.inspect_repository(repo, options), None, client.get_issue("TST-1"),
                harness.parse_harness_spec("codex"), harness.parse_harness_spec("agy"), options, cleanup,
            )
            if isinstance(run, dict):
                self.fail(f"Preflight returned an early result: {run}")
            checkpoint = json.loads((run.run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint, json.loads(run.latest_file.read_text(encoding="utf-8")))
            self.assertEqual(checkpoint["stage"], "preflight_ok")
            self.assertEqual(checkpoint["base_sha"], _git(repo, "rev-parse", "HEAD"))
            self.assertEqual(client.get_issue("TST-1")["state"]["name"], "Todo")

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
