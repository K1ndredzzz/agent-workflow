"""Behavioral tests for the L2 process runtime."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_dispatch import config, git_ops, harness, process
from tests.helpers import IsolatedDispatchCase, WORKFLOW_ROOT, _git, _write_exec

PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"


class TestProcessRuntime(IsolatedDispatchCase):
    def test_run_cmd_bounded_buffer_caps_memory_while_streaming_full_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            out_log = d / "out.log"
            # Generate 16KB of numbered lines with a patched 2KB DISPLAY_OUTPUT_CAP
            code = "for i in range(400):\n    print(f'line-{i:04d}-' + 'x' * 32)\n"
            with mock.patch.object(config, "DISPLAY_OUTPUT_CAP", 2048):
                res = process.run_cmd(
                    [sys.executable, "-B", "-c", code],
                    cwd=d,
                    cap_output=True,
                    stdout_path=out_log,
                )
            self.assertIn("...[truncated]...", res.stdout)
            self.assertIn("line-0000-", res.stdout)
            self.assertIn("line-0399-", res.stdout)
            self.assertLessEqual(len(res.stdout), 2500)
            # Full un-truncated output still streamed to disk log
            self.assertEqual(len(out_log.read_text(encoding="utf-8").splitlines()), 400)

    def test_api_key_headers_are_redacted_in_streamed_logs(self) -> None:
        token = "opaque" + "-synthetic!value"
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            for header in ("X-API-Key", "x-goog-api-key", "X-Api-kEy"):
                event = {"request": {"headers": {header: token}}, "status": "ok"}
                plain = f"Request header: {header}: {token}"
                with contextlib.ExitStack() as stack:
                    writer = process.RedactedLog(folder / "log", stack)
                    writer.write((plain + "\n" + json.dumps(event) + "\n").encode())
                self.assertNotIn(token, (folder / "log").read_text())

    def test_streamed_logs_redact_credentials_before_persistence_and_on_timeout(self) -> None:
        bearer = "invented-only-example"
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"UNIT_API_KEY": "synthetic-opaque-value"}):
            d = Path(td)
            payload = f"Bearer {bearer}\n{os.environ['UNIT_API_KEY']}\n{PEM_BEGIN}\nexample-key-body\n{PEM_END}\n"
            code = ("import os,sys,time\nfor line in sys.stdin.buffer:\n"
                    " for fd in (1,2):\n  os.write(fd,line[:5]); os.write(fd,line[5:])\n")
            for timeout in (False, True):
                args = [sys.executable, "-B", "-c", code + ("time.sleep(10)" if timeout else "")]
                kw = dict(stdin_text=payload, cwd=d, stdout_path=d / "out", stderr_path=d / "err",
                          timeout=0.5 if timeout else 5)
                if timeout:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.run_cmd(args, **kw)
                else:
                    process.run_cmd(args, **kw)
                for file in (d / "out", d / "err"):
                    body = file.read_text()
                    self.assertIn("<REDACTED_SECRET>", body)
                    for secret in (bearer, "example-key-body", os.environ["UNIT_API_KEY"]):
                        self.assertNotIn(secret, body)

    def test_child_git_config_cannot_inherit_auth_rewrites_or_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / ".gitconfig").write_text('[url "https://fictional-token@invalid.example/"]\n    insteadOf = local:!\n')
            with mock.patch.dict(os.environ, {"HOME": td, "AGENT_DISPATCH_CACHE_ROOT": str(d / "cache"),
                                               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_VALUE_0": "fictional-token",
                                               "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_TRACE": "1"}):
                env = process.build_child_env()
            self.assertFalse({"GIT_CONFIG_COUNT", "GIT_CONFIG_VALUE_0", "GIT_TRACE"} & env.keys())
            self.assertEqual(Path(env["GIT_CONFIG_GLOBAL"]).read_text(), "")
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_child_env_stripping_isolated_without_polluting_os_environ(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {
                    "AGENT_DISPATCH_CACHE_ROOT": td,
                    "LINEAR_API_KEY": "synthetic-linear-credential",
                    "GH_TOKEN": "synthetic-gh-credential",
                    "GITHUB_TOKEN": "synthetic-github-credential",
                    "SSH_AUTH_SOCK": "/tmp/fake-ssh.sock",
                    "CODEX_SUB2API_API_KEY": "sub2api_test_key",
                },
            ):
                child_env = process.build_child_env(role="child")
                self.assertEqual(child_env["AGENT_DISPATCH_CHILD"], "1")
                self.assertEqual(child_env["GIT_TERMINAL_PROMPT"], "0")
                self.assertTrue(child_env["GH_CONFIG_DIR"].startswith(td))
                for k in ("LINEAR_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "CODEX_SUB2API_API_KEY"):
                    self.assertNotIn(k, child_env)
                codex_env = process.build_child_env(role="codex")
                self.assertIn("CODEX_SUB2API_API_KEY", codex_env)
                self.assertNotIn("LINEAR_API_KEY", codex_env)
                self.assertNotIn("GH_TOKEN", codex_env)
                self.assertNotIn("SSH_AUTH_SOCK", codex_env)

    def test_redacted_log_json_truncated_pem_does_not_drop_subsequent_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "stream.log"
            payload = (
                json.dumps({"type": "tool_result", "output": f"{PEM_BEGIN}\nMIIE_secret_body"}) + "\n"
                + json.dumps({"type": "thread.started", "thread_id": "11111111-2222-3333-4444-555555555555"}) + "\n"
                + "plain line after json\n"
            )
            with contextlib.ExitStack() as stack:
                logger = process.RedactedLog(log_path, stack)
                logger.write(payload.encode("utf-8"))
                logger.write(b"")
            written = log_path.read_text(encoding="utf-8")
            self.assertNotIn("MIIE_secret_body", written)
            self.assertIn("<REDACTED_SECRET>", written)
            self.assertIn("11111111-2222-3333-4444-555555555555", written)
            self.assertIn("plain line after json", written)

    def test_truncation_preserves_pem_redaction_and_bounds_unterminated_lines(self) -> None:
        # Make the opening delimiter land in the discarded middle of the raw display buffer.
        padding = "ordinary diagnostic\n" * 20
        payload = f"{padding}{PEM_BEGIN}\n" + "synthetic-pem-body\n" * 20 + f"{PEM_END}\nfinished\n"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "verify.log"
            with mock.patch.object(config, "DISPLAY_OUTPUT_CAP", 256):
                result = process.run_cmd(
                    [sys.executable, "-B", "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                    stdin_text=payload, stdout_path=path,
                )
            for output in (path.read_text(), result.stdout):
                self.assertNotIn("synthetic-pem-body", output)
                self.assertIn("finished", output)
            with contextlib.ExitStack() as stack, mock.patch.object(process.RedactedLog, "line_limit", 64):
                sink = process.RedactedLog(path, stack)
                for _ in range(100):
                    sink.write(b"x" * 32)
                    self.assertLessEqual(len(sink.pending), 64)
                sink.write(b"-----BE")
                sink.write(b"GIN PRIVATE KEY-----\nsynthetic-pem-body\n-----END PRIVATE KEY-----\nfinished\n")
                sink.write(b"")
            self.assertNotIn("synthetic-pem-body", path.read_text())
            self.assertIn("oversized diagnostic line omitted", path.read_text())
            self.assertIn("finished", path.read_text())

            # Oversized CERTIFICATE block or oversized JSON containing a truncated PRIVATE KEY header
            # must NOT permanently poison in_pem and drop subsequent lines!
            with contextlib.ExitStack() as stack, mock.patch.object(process.RedactedLog, "line_limit", 64):
                sink = process.RedactedLog(path, stack)
                sink.write(b"x" * 70 + b"-----BEGIN CERTIFICATE-----\nkept after cert\n")
                sink.write(b'{"type":"tool","out":"' + b"y" * 70 + PEM_BEGIN.encode() + b'\\nsecret"}\n')
                sink.write(b'{"type":"thread.started","thread_id":"aaaa-bbbb"}\n')
                sink.write(b"")
            logged = path.read_text()
            self.assertIn("kept after cert", logged)
            self.assertIn("aaaa-bbbb", logged)

    def test_oversized_broken_json_hides_following_plain_pem_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "verify.log"
            with contextlib.ExitStack() as stack:
                sink = process.RedactedLog(log, stack)
                sink.write(b'{"error":"' + b"x" * sink.line_limit)
                sink.write(b"-----BEGIN PRIV")
                sink.write(b"ATE KEY-----\n")
                sink.write(b"onlySyntheticBodyData\n-----END PRIVATE KEY-----\nsafe diagnostic\n")
            self.assertNotIn("onlySyntheticBodyData", log.read_text())
            self.assertIn("safe diagnostic", log.read_text())

    def test_run_cmd_replaces_invalid_utf8(self) -> None:
        # Non-UTF-8 subprocess output MUST NOT raise UnicodeDecodeError!
        res_bin = process.run_cmd([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xffabc')"], env_role="orchestrator")
        self.assertEqual(res_bin.returncode, 0)
        self.assertIn("abc", res_bin.stdout)

    def test_git_cmd_disables_host_integrations(self) -> None:
        command = [
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=",
            "-c", "core.quotePath=false", "--no-pager",
        ]
        self.assertEqual(process.git_cmd("status"), command + ["status"])
        self.assertEqual(
            process.git_cmd("diff", "--name-only", attr_source="HEAD"),
            command + ["--attr-source=HEAD", "diff", "--name-only"],
        )

    def test_atomic_write_replaces_the_file_and_hashes_the_new_contents(self) -> None:
        target = self.sandbox / "checkpoints" / "latest.json"
        process.atomic_write_text(target, "previous")
        old_file = self.sandbox / "previous"
        old_file.hardlink_to(target)
        process.atomic_write_text(target, "abc")

        self.assertEqual(old_file.read_text(encoding="utf-8"), "previous")
        self.assertEqual(target.read_text(encoding="utf-8"), "abc")
        digest = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        self.assertEqual(process.sha256_file(target), digest)
        self.assertEqual(process.sha256_text("abc"), digest)

    def test_shutdown_interrupts_contended_file_lock(self) -> None:
        path = Path(os.environ["AGENT_DISPATCH_CACHE_ROOT"]) / "verify.lock"
        self.addCleanup(process.reset_shutdown)
        self.assertFalse(process.is_locked(path))
        with process.file_lock(path):
            self.assertTrue(process.is_locked(path))
            with self.assertRaisesRegex(RuntimeError, "already locked"):
                with process.file_lock(path, blocking=False):
                    self.fail("nonblocking waiter acquired the lock")
            process.request_shutdown(signal.SIGTERM)
            with self.assertRaises(SystemExit) as raised:
                with process.file_lock(path):
                    self.fail("shutdown waiter acquired the lock")
            self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertFalse(process.is_locked(path))

    def test_all_shutdown_signals_during_child_registration_reap_the_process(self) -> None:
        # Deliver the signal after Popen starts the child and before the runner can register it.
        probe = """
import os
import signal
import subprocess
import sys
from unittest import mock

from agent_dispatch import process

process.install_signal_handlers()
signum = int(sys.argv[1])
popen = subprocess.Popen
pids = []

def interrupt_after_spawn(*args, **kwargs):
    child = popen(*args, **kwargs)
    pids.append(child.pid)
    os.kill(os.getpid(), signum)
    return child

with mock.patch.object(subprocess, "Popen", side_effect=interrupt_after_spawn):
    try:
        process.run_cmd([sys.executable, "-B", "-c", "import time; time.sleep(4)"])
    except (SystemExit, KeyboardInterrupt) as exc:
        assert process.is_shutdown_requested() and pids
        if signum == signal.SIGINT:
            assert isinstance(exc, KeyboardInterrupt)
        else:
            assert isinstance(exc, SystemExit) and exc.code == 128 + signum
        try:
            os.kill(pids[0], 0)
        except ProcessLookupError:
            sys.exit(0)
sys.exit(1)
"""
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = subprocess.run(
                    [sys.executable, "-B", "-c", probe, str(sig.value)],
                    cwd=WORKFLOW_ROOT, capture_output=True, text=True, timeout=6,
                )
                self.assertEqual(child.returncode, 0, child.stderr)


class TestGitOps(IsolatedDispatchCase):
    def test_worktree_owner_and_repository_lock_identity(self) -> None:
        repo = self.sandbox / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-m", "base")

        worktree, branch, base = git_ops.prepare_worktree(repo, "TST-1", "main", issue_uuid="issue-1")
        self.assertEqual(branch, "feat/tst-1")
        self.assertEqual(base, _git(repo, "rev-parse", "HEAD"))
        self.assertEqual(git_ops.canonical_repo_identity(repo), git_ops.canonical_repo_identity(worktree))
        self.assertEqual(git_ops.ticket_lock_path(repo, "issue-1"), git_ops.ticket_lock_path(worktree, "issue-1"))
        repo_key = git_ops.repo_cache_key(repo)
        self.assertEqual(worktree, self.sandbox / "cache" / "worktrees" / repo_key / "TST-1")
        self.assertEqual(git_ops.read_worktree_lock_owner(repo, repo_key, worktree), ("issue-1", branch, {}, ""))
        self.assertEqual(git_ops.prepare_worktree(repo, "TST-1", "main", issue_uuid="issue-1"),
                         (worktree, branch, base))
        with self.assertRaisesRegex(RuntimeError, "Worktree ownership mismatch"):
            git_ops.prepare_worktree(repo, "TST-1", "main", issue_uuid="other-issue")


    def test_parse_github_remote_urls(self) -> None:
        # 5. GitHub URL parser handles SSH-over-443 & trailing .git/
        self.assertEqual(
            git_ops.parse_github_owner_repo_url("ssh://git@ssh.github.com:443/K1ndredzzz/agent-workflow.git/"),
            "K1ndredzzz/agent-workflow",
        )
        self.assertEqual(
            git_ops.parse_github_owner_repo_url("https://github.com/K1ndredzzz/agent-workflow.git/"),
            "K1ndredzzz/agent-workflow",
        )
        self.assertIsNone(git_ops.parse_github_owner_repo_url("git@github-work:org/repo.git"))

    def test_dirty_docs_allows_behind_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bare = Path(td) / "origin.git"
            repo = Path(td) / "demo"
            _git(Path(td), "init", "--bare", "-b", "main", str(bare))
            _git(Path(td), "clone", str(bare), str(repo))
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "GLOSSARY.md").write_text("# Glossary\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            _git(repo, "push", "-u", "origin", "main")

            # Simulate docs PR merged on origin/main while local main is behind -> MUST NOT flag dirty docs!
            other = Path(td) / "other"
            _git(Path(td), "clone", str(bare), str(other))
            _git(other, "config", "user.email", "t@example.com")
            _git(other, "config", "user.name", "T")
            (other / "GLOSSARY.md").write_text("# Glossary v2\n", encoding="utf-8")
            _git(other, "add", "GLOSSARY.md")
            _git(other, "commit", "-m", "docs PR")
            _git(other, "push", "origin", "main")
            _git(repo, "fetch", "origin", "main")
            self.assertEqual(git_ops.check_dirty_docs_in_main_repo(repo, "main"), [])


class TestHarness(IsolatedDispatchCase):
    def write_harness(self, name: str, source: str) -> None:
        _write_exec(self.sandbox / name, f"#!{sys.executable}\n{source}")
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{self.sandbox}{os.pathsep}{os.environ['PATH']}"}))

    def test_harness_model_policy_rejects_unlisted_names_and_tiers(self) -> None:
        for spec in ("agy:opus", "agy:claude-sonnet", "agy:gemini-3", "agy:neon@bogus",
                     "codex:6.1-sol", "opencode:sub2api/custom", "pi:other/gpt-6.2-astra"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                harness.parse_harness_spec(spec)
        for name in ("codex", "opencode", "pi"):
            for model in ("gpt-6.2-sol", "gpt-6.2-astra"):
                self.assertTrue(harness.parse_harness_spec(f"{name}:{model}").model.endswith(model))
        for model in ("neon", "carbon", "argon", "hydrogen"):
            self.assertTrue(harness.parse_harness_spec(f"agy:{model}").model.startswith("gemini-4-"))


    def test_nested_authorization_headers_are_redacted_in_structured_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            token = "syntheticopaque"
            event = {"request": {"AUTHORIZATION": "Bearer " + token,
                                 "Proxy-Authorization": "Basic " + token},
                     "description": "Support bearer authentication"}
            log = folder / "log"
            with contextlib.ExitStack() as stack:
                writer = process.RedactedLog(log, stack)
                writer.write((json.dumps(event) + "\n").encode())
            harness.append_session_trace(folder, event, None)
            for path in (log, folder / "sessions.jsonl"):
                saved = json.loads(path.read_text())
                self.assertNotIn(token, json.dumps(saved))
                self.assertEqual(saved["description"], event["description"])

    def test_codex_rejects_malformed_final_assistant_after_valid_verdict(self) -> None:
        for bad_text in (None, {"verdict": "pass"}, ["text"]):
            with self.subTest(bad_text=bad_text):
                items = [{"type": "item.completed", "item": {"type": "agent_message", "text": val}}
                         for val in ('{"verdict":"pass"}', bad_text)]
                with self.assertRaises(ValueError):
                    harness.parse_codex_ndjson("\n".join(map(json.dumps, [*items, {"type": "turn.completed"}])))

    def test_ndjson_incomplete_final_turn_cannot_reuse_prior_verdict(self) -> None:
        text = '{"verdict":"pass"}'
        previous = {"role": "assistant", "content": text, "stopReason": "stop"}
        failed = {"role": "assistant", "content": [], "stopReason": "error"}
        for final in ({"type": "message_update", "message": previous},
                      {"type": "message_end", "message": failed},
                      {"type": "agent_end", "messages": [previous, failed]}):
            with self.subTest(event=final["type"]), self.assertRaises(ValueError):
                harness.parse_pi_ndjson("\n".join(map(json.dumps, [{"type": "message_end", "message": previous}, final])))
        oc = [{"type": "text", "part": {"text": text}}, {"type": "step_finish", "part": {"reason": "stop"}},
              {"type": "step_start"}, {"type": "step_finish", "part": {"reason": "length"}}]
        with self.assertRaises(ValueError):
            harness.parse_opencode_ndjson("\n".join(map(json.dumps, oc)))
        cx = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
              {"type": "turn.completed"}, {"type": "turn.started"}]
        with self.assertRaises(ValueError):
            harness.parse_codex_ndjson("\n".join(map(json.dumps, cx)))

    def test_harness_aliases_and_heterogeneous_reviewer(self) -> None:
        w_codex = harness.parse_harness_spec("codex:sol@xhigh")
        self.assertEqual(w_codex.label, "codex:gpt-6.2-sol@xhigh")
        self.assertEqual(harness.default_reviewer_for(w_codex).label, "agy:gemini-4-neon-xhigh")

        w_agy = harness.parse_harness_spec("agy:neon@high")
        self.assertEqual(w_agy.model, "gemini-4-neon-high")
        self.assertEqual(w_agy.effort, "")
        self.assertEqual(harness.default_reviewer_for(w_agy).label, "codex:gpt-6.2-sol@xhigh")

        w_oc = harness.parse_harness_spec("opencode:astra")
        self.assertEqual(w_oc.model, "sub2api/gpt-6.2-astra")
        self.assertEqual(harness.default_reviewer_for(w_oc).label, "agy:gemini-4-neon-xhigh")

        w_pi = harness.parse_harness_spec("pi:sol@high")
        self.assertEqual(w_pi.model, "sub2api/gpt-6.2-sol")
        self.assertEqual(w_pi.effort, "high")

    def test_final_array_and_corrupt_protocol_event_cannot_approve_a_review(self) -> None:
        verdict = {"axis": "standards", "verdict": "pass", "hard_findings": [], "suggestions": [],
                   "test_integrity_violation": False, "summary": "ok"}
        for output in (json.dumps([verdict]), json.dumps(verdict) + "\nFinal: []"):
            stream = [{"type": "text", "part": {"text": output}}, {"type": "step_finish", "part": {"reason": "stop"}}]
            final_text = harness.parse_opencode_ndjson("\n".join(map(json.dumps, stream)))
            with self.assertRaises(ValueError):
                harness.validate_schema(harness.extract_json_object(final_text), config.REVIEW_SCHEMA)
        interim = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(verdict)}}
        stream = "\n".join([json.dumps(interim), '{"type":"item.completed","item":', '{"type":"turn.completed"}'])
        with self.assertRaises(ValueError):
            harness.parse_codex_ndjson(stream)

    def test_truncated_final_object_cannot_reuse_draft_or_nested_verdict(self) -> None:
        draft = '{"verdict":"pass"}'
        for tail in ('{"verdict":"fail"', '{"details":{"verdict":"pass"}', '{', '{"summary":"unfinished'):
            with self.subTest(tail=tail):
                for response in (draft + "\nFinal answer:\n" + tail, "```json\n" + draft + "\n```\n" + tail):
                    with self.assertRaises(ValueError):
                        harness.extract_json_object(response)
        self.assertEqual(harness.extract_json_object('draft {"bad": } final {"verdict":"fail"} done'),
                         {"verdict": "fail"})

    def test_extract_json_raw_decode_with_trailing_logs(self) -> None:
        raw = 'noise {"a": 1}\nfinal: {"status": "completed", "summary": "ok"} trailing log line\n'
        self.assertEqual(harness.extract_json_object(raw), {"status": "completed", "summary": "ok"})

    def test_pi_ndjson_ignores_user_schema_and_takes_last_assistant(self) -> None:
        events = [
            json.dumps({"type": "message_end", "message": {"role": "user", "content": json.dumps(config.WORKER_SCHEMA)}}),
            json.dumps({"type": "message_end", "message": {"role": "toolResult", "content": '{"noise": true}'}}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": '{"status":"completed","summary":"done","tests_run":["pytest"],"discovered_issues":[],"blockers":""}'}]}}),
        ]
        txt = harness.parse_pi_ndjson("\n".join(events))
        obj = harness.validate_schema(harness.extract_json_object(txt), config.WORKER_SCHEMA)
        self.assertEqual(obj["summary"], "done")

    def test_schema_validator_rejects_missing_and_wrong_axis(self) -> None:
        with self.assertRaises(ValueError):
            harness.validate_schema({"axis": "standards", "verdict": "pass"}, config.REVIEW_SCHEMA, expected_axis="standards")
        valid_std = {
            "axis": "standards",
            "verdict": "pass",
            "hard_findings": [],
            "suggestions": [],
            "test_integrity_violation": False,
            "summary": "Clean.",
        }
        with self.assertRaises(ValueError):
            harness.validate_schema(valid_std, config.REVIEW_SCHEMA, expected_axis="spec")
        self.assertEqual(harness.validate_schema(valid_std, config.REVIEW_SCHEMA, expected_axis="standards")["verdict"], "pass")

    def test_oversized_harness_output_stops_and_keeps_session_trace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "codex"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' '\"session id: 11111111-2222-3333-4444\"' >&2\n"
                            "exec head -c 262144 /dev/zero\n")
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.defpath}), \
                    mock.patch.object(config, "HARNESS_OUTPUT_LIMIT", 32768):
                with self.assertRaisesRegex(RuntimeError, "output limit"):
                    harness.invoke_harness(harness.parse_harness_spec("codex:sol"), "review", worktree=root, run_dir=root,
                                      schema=config.REVIEW_SCHEMA, schema_file=root / "schema", prompt_file=root / "prompt",
                                      out_file=root / "result", read_only=True)
            end = json.loads((root / "sessions.jsonl").read_text().splitlines()[-1])
            self.assertEqual(end["status"], "error")
            self.assertEqual(end["session_id"], "11111111-2222-3333-4444")
            self.assertFalse((root / "result").exists())

    def test_removed_agy_aliases(self) -> None:
        # 1. Opus/Claude aliases completely removed from agy harness
        self.assertNotIn("opus", config.HARNESS_ALIASES["agy"])
        self.assertNotIn("sonnet", config.HARNESS_ALIASES["agy"])
        self.assertEqual(harness.parse_harness_spec("agy:neon").vendor, "google")

    def test_opencode_final_verdict_overrides_draft(self) -> None:
        # 2. OpenCode NDJSON: earlier fenced `pass` MUST NEVER override trailing bare `fail`!
        oc_stream = "\n".join(
            [
                json.dumps({"type": "step_start", "sessionID": "ses_1"}),
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "ses_1",
                        "part": {
                            "messageID": "m1",
                            "text": 'Draft check:\n```json\n{"axis":"standards","verdict":"pass","hard_findings":[],"suggestions":[],"test_integrity_violation":false,"summary":"looks ok"}\n```',
                        },
                    }
                ),
                json.dumps({"type": "step_start", "sessionID": "ses_1"}),
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "ses_1",
                        "part": {
                            "messageID": "m2",
                            "text": 'Final verdict:\n{"axis":"standards","verdict":"fail","hard_findings":["Null deref"],"suggestions":[],"test_integrity_violation":false,"summary":"Found bug"}',
                        },
                    }
                ),
                json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
            ]
        )
        parsed_txt = harness.parse_opencode_ndjson(oc_stream)
        verdict_obj = harness.validate_schema(harness.extract_json_object(parsed_txt), config.REVIEW_SCHEMA, expected_axis="standards")
        self.assertEqual(verdict_obj["verdict"], "fail")
        # Also verify extract_json_object alone when BOTH fenced pass and trailing bare fail are in the same message!
        same_msg = (
            '```json\n{"axis":"spec","verdict":"pass","hard_findings":[],"suggestions":[],"test_integrity_violation":false,"summary":"early"}\n```\n'
            'Actually failed:\n{"axis":"spec","verdict":"fail","hard_findings":["Missing AC"],"suggestions":[],"test_integrity_violation":false,"summary":"final"}'
        )
        self.assertEqual(harness.extract_json_object(same_msg)["verdict"], "fail")


    def test_review_prompt_result_and_trace_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            secret = "lin_api_" + "syntheticvalue123456"
            output = {"status": "completed", "summary": secret, "tests_run": [], "discovered_issues": [], "blockers": ""}
            events = [{"type": "thread.started", "thread_id": "aaaa-bbbb"},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(output)}},
                      {"type": "turn.completed"}]
            self.write_harness("codex", f"""import json, sys
from pathlib import Path
Path("argv.json").write_text(json.dumps(sys.argv))
Path("stdin.txt").write_text(sys.stdin.read())
print({chr(10).join(map(json.dumps, events))!r})
""")
            result = harness.invoke_harness(harness.parse_harness_spec("codex"), secret, worktree=d, run_dir=d,
                                            schema=config.WORKER_SCHEMA, schema_file=d / "schema", prompt_file=d / "prompt",
                                            out_file=d / "result", read_only=False, session_label=secret)
            self.assertEqual(result["summary"], "<REDACTED_SECRET>")
            self.assertNotIn(secret, (d / "stdin.txt").read_text())
            cmd = json.loads((d / "argv.json").read_text())
            self.assertIn("--json", cmd)
            self.assertNotIn("-o", cmd)
            for path in (d / "prompt", d / "result", d / "sessions.jsonl"):
                self.assertNotIn(secret, path.read_text())

    def test_opencode_attaches_files_before_double_dash_and_captures_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            wt = d / "wt"
            run_dir = d / "run"
            wt.mkdir()
            run_dir.mkdir()
            extra_diff = run_dir / "diff-r1.patch"
            extra_diff.write_text("diff --git a/a.py b/a.py\n+x = 1\n", encoding="utf-8")
            ndjson = "\n".join(
                [
                    json.dumps({"type": "step_start", "sessionID": "ses_unit_12345"}),
                    json.dumps(
                        {
                            "type": "text",
                            "sessionID": "ses_unit_12345",
                            "part": {
                                "text": json.dumps(
                                    {
                                        "axis": "standards",
                                        "verdict": "pass",
                                        "hard_findings": [],
                                        "suggestions": [],
                                        "test_integrity_violation": False,
                                        "summary": "Clean diff.",
                                    }
                                )
                            },
                        }
                    ),
                    json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
                ]
            )
            self.write_harness("opencode", f"""import json, sys
from pathlib import Path
with Path("commands.jsonl").open("a") as output:
    output.write(json.dumps(sys.argv) + "\\n")
print({ndjson!r})
""")

            traces: list[dict[str, Any]] = []
            res = harness.invoke_harness(
                harness.parse_harness_spec("opencode:sol@low"),
                "Review diff",
                worktree=wt,
                run_dir=run_dir,
                schema=config.REVIEW_SCHEMA,
                schema_file=run_dir / "s.json",
                prompt_file=run_dir / "p.md",
                out_file=run_dir / "o.json",
                read_only=True,
                expected_axis="standards",
                session_label="review-standards-r1",
                trace_sink=traces,
                attach_files=[extra_diff, extra_diff],
            )
            captured_cmds = [json.loads(line) for line in (wt / "commands.jsonl").read_text().splitlines()]
            self.assertEqual(res["verdict"], "pass")
            self.assertEqual(len(captured_cmds), 1)
            cmd = captured_cmds[0]
            self.assertIn("--agent", cmd)
            self.assertEqual(cmd[cmd.index("--agent") + 1], "plan")
            # Deduplicated -f flags precede `--` and the positional message
            dd_idx = cmd.index("--")
            f_vals = [cmd[i + 1] for i, tok in enumerate(cmd[:dd_idx]) if tok == "-f"]
            self.assertEqual(f_vals, [str(run_dir / "p.md"), str(extra_diff)])
            self.assertEqual(dd_idx, len(cmd) - 2)
            self.assertEqual(traces[0]["session_id"], "ses_unit_12345")
            self.assertEqual(traces[0]["resume_cmd"], "opencode -s ses_unit_12345")

    def test_pi_timeout_preserves_session_trace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 2. Pi timeout MUST preserve session_id in sessions.jsonl!
            run_dir = d / "pi-run"
            run_dir.mkdir()
            traces: list[dict[str, Any]] = []
            self.write_harness("pi", "import time\ntime.sleep(10)\n")
            with self.assertRaises(subprocess.TimeoutExpired):
                harness.invoke_harness(
                    harness.parse_harness_spec("pi:sol@low"),
                    "check",
                    worktree=d,
                    run_dir=run_dir,
                    schema=config.REVIEW_SCHEMA,
                    schema_file=run_dir / "s.json",
                    prompt_file=run_dir / "p.md",
                    out_file=run_dir / "o.json",
                    read_only=True,
                    expected_axis="standards",
                    session_label="pi-timeout-test",
                    trace_sink=traces,
                    timeout=0.2,
                )
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0]["status"], "timeout")
            self.assertTrue(traces[0].get("session_id"), "Pi timeout MUST keep generated session_id!")

    def test_agy_worker_disables_slash_commands_without_plan_mode(self) -> None:
        repo = self.sandbox
        run_dir = self.sandbox / "run"
        # agy worker mode MUST pass --disable-slash-commands and NOT --mode plan!
        out = {
            "conversation_id": "11111111-1111-1111-1111-111111111111",
            "status": "SUCCESS",
            "structured_output": {"status": "completed", "summary": "ok", "tests_run": [], "discovered_issues": [], "blockers": ""},
        }
        self.write_harness("agy", f"""import json, sys
from pathlib import Path
Path("agy-argv.json").write_text(json.dumps(sys.argv))
print({json.dumps(out)!r})
""")
        harness.invoke_harness(
            harness.parse_harness_spec("agy:carbon"),
            "work",
            worktree=repo,
            run_dir=run_dir,
            schema=config.WORKER_SCHEMA,
            schema_file=run_dir / "ws.json",
            prompt_file=run_dir / "wp.md",
            out_file=run_dir / "wo.json",
            read_only=False,
            session_label="agy-worker-test",
        )
        cmd = json.loads((repo / "agy-argv.json").read_text())
        self.assertIn("--disable-slash-commands", cmd)
        self.assertNotIn("plan", cmd)

    def test_codex_tolerates_non_protocol_diagnostic_lines(self) -> None:
        # Stray non-JSON line starting with '{' must not break NDJSON parsing
        verdict = json.dumps({"status": "completed"})
        codex_out = "\n".join([
            "{non-json diagnostic header",
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": verdict}}),
            json.dumps({"type": "turn.completed"}),
        ])
        self.assertEqual(harness.parse_codex_ndjson(codex_out), verdict)

    def test_pi_session_replay_preserves_native_entries_and_redacts_on_interrupt(self) -> None:
        secret = "lin_api_" + "syntheticpioutput12345"
        entry = {"type": "message", "id": "a1", "parentId": None, "message": {"content": secret}}
        session_dir = self.sandbox / "pi-sessions"
        data = json.dumps(entry) + "\n" + '{"type":'
        self.write_harness("pi", f"""import os, signal, sys, time
from pathlib import Path
native = Path(sys.argv[sys.argv.index("--session") + 1])
assert os.readlink(native).startswith("/memfd:")
with native.open("a") as session:
    session.write({data!r})
assert not list(Path({str(session_dir)!r}).iterdir())
os.kill(os.getppid(), signal.SIGINT)
time.sleep(10)
""")
        previous_handler = signal.signal(signal.SIGINT, lambda signum, frame: process.request_shutdown(signum))
        self.addCleanup(signal.signal, signal.SIGINT, previous_handler)
        self.addCleanup(process.reset_shutdown)
        with self.assertRaises(KeyboardInterrupt):
            harness.invoke_harness(
                harness.parse_harness_spec("pi"), "check", worktree=self.sandbox, run_dir=self.sandbox,
                schema=config.REVIEW_SCHEMA, schema_file=self.sandbox / "schema", prompt_file=self.sandbox / "prompt",
                out_file=self.sandbox / "result", read_only=True, timeout=5,
            )
        archive, = session_dir.glob("*.jsonl")
        header, message = map(json.loads, archive.read_text().splitlines())
        self.assertEqual(header["version"], 3)
        self.assertEqual(message["id"], "a1")
        self.assertEqual(message["message"]["content"], "<REDACTED_SECRET>")

    def test_pi_replay_skips_malformed_lines_without_masking_failure(self) -> None:
        valid_entry = {"type": "message", "id": "m1", "message": {"content": "ok"}}
        data = "\n{malformed json line\n" + json.dumps(valid_entry) + "\n"
        self.write_harness("pi", f"""import sys
from pathlib import Path
native = Path(sys.argv[sys.argv.index("--session") + 1])
with native.open("a") as session:
    session.write({data!r})
sys.exit("outer harness failure")
""")
        with self.assertRaisesRegex(RuntimeError, "outer harness failure"):
            harness.invoke_harness(
                harness.parse_harness_spec("pi"), "check", worktree=self.sandbox, run_dir=self.sandbox,
                schema=config.REVIEW_SCHEMA, schema_file=self.sandbox / "schema", prompt_file=self.sandbox / "prompt",
                out_file=self.sandbox / "result", read_only=True, timeout=5,
            )
        archive, = (self.sandbox / "pi-sessions").glob("*.jsonl")
        lines = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["id"], "m1")


if __name__ == "__main__":
    unittest.main()
