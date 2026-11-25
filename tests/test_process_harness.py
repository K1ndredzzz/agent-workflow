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
from unittest import mock

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_dispatch import config, process
from tests.helpers import IsolatedDispatchCase, WORKFLOW_ROOT

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


if __name__ == "__main__":
    unittest.main()
