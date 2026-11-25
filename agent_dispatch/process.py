"""Subprocess execution, signal coordination, and atomic filesystem operations."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from agent_dispatch import config
from agent_dispatch.secrets import redact_secrets, safe_json


# The signal handler reads an immutable PGID snapshot and writes a plain integer flag.
_SHUTDOWN_SIGNAL: int = 0
_ACTIVE_PGIDS_LOCK = threading.RLock()
_ACTIVE_PGIDS: set[int] = set()
_ACTIVE_PGIDS_SNAPSHOT: tuple[int, ...] = ()


def register_active_pgid(pid: int) -> None:
    global _ACTIVE_PGIDS_SNAPSHOT
    with _ACTIVE_PGIDS_LOCK:
        _ACTIVE_PGIDS.add(pid)
        _ACTIVE_PGIDS_SNAPSHOT = tuple(_ACTIVE_PGIDS)


def unregister_active_pgid(pid: int) -> None:
    global _ACTIVE_PGIDS_SNAPSHOT
    with _ACTIVE_PGIDS_LOCK:
        _ACTIVE_PGIDS.discard(pid)
        _ACTIVE_PGIDS_SNAPSHOT = tuple(_ACTIVE_PGIDS)


def request_shutdown(signum: int) -> bool:
    """Record the first shutdown signal without acquiring a lock."""
    global _SHUTDOWN_SIGNAL
    if _SHUTDOWN_SIGNAL:
        return False
    _SHUTDOWN_SIGNAL = signum
    return True


def reset_shutdown() -> None:
    """Clear the shutdown flag for isolated tests."""
    global _SHUTDOWN_SIGNAL
    _SHUTDOWN_SIGNAL = 0


def is_shutdown_requested() -> bool:
    return _SHUTDOWN_SIGNAL != 0


def check_shutdown() -> None:
    sig = _SHUTDOWN_SIGNAL
    if sig != 0:
        if sig == signal.SIGINT:
            raise KeyboardInterrupt("Interrupted by SIGINT")
        raise SystemExit(128 + sig)


def interruptible_sleep(seconds: float, *, ignore_shutdown: bool = False) -> None:
    if seconds <= 0:
        if not ignore_shutdown:
            check_shutdown()
        return
    if ignore_shutdown:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        check_shutdown()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.1))


def kill_all_active_children(*, wait_sigkill: bool = True) -> None:
    # Read immutable tuple reference without acquiring any lock (safe from a signal handler!)
    pgids = _ACTIVE_PGIDS_SNAPSHOT
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    if pgids and wait_sigkill:
        time.sleep(0.3)
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass


def install_signal_handlers() -> None:
    def _handler(signum: int, _frame: Any) -> None:
        if request_shutdown(signum):
            # Callers handle the SIGKILL wait and reconciliation.
            kill_all_active_children(wait_sigkill=False)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except ValueError:
            pass


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def file_lock(lock_path: Path, *, blocking: bool = True, ignore_shutdown: bool = False) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    try:
        while True:
            if not ignore_shutdown:
                check_shutdown()
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not blocking:
                    raise RuntimeError(f"Resource is already locked by another active agent-dispatch process: {lock_path.name}") from exc
                time.sleep(0.1)
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} ts={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def is_locked(lock_path: Path) -> bool:
    if not lock_path.is_file():
        return False
    fh = lock_path.open("r+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        fh.close()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {redact_secrets(msg)}", file=sys.stderr, flush=True)


def git_cmd(*args: str, attr_source: str | None = None) -> list[str]:
    """Safe orchestrator git invocation: disables repo hooks, fsmonitor, pager, and path quoting."""
    cmd = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=",
        "-c",
        "core.quotePath=false",
        "--no-pager",
    ]
    if attr_source:
        cmd.append(f"--attr-source={attr_source}")
    cmd.extend(args)
    return cmd


def ensure_sanitized_gitconfig() -> tuple[Path, Path]:
    """Give children an empty Git credential store without copying host config."""
    base = config._cache_root() / "child-sandbox-env"
    gh_empty = base / "empty-gh-config"
    gh_empty.mkdir(parents=True, exist_ok=True)
    # /dev/null also avoids publishing credentials from URL rewrites or Git aliases.
    return gh_empty, Path(os.devnull)


def build_child_env(*, role: str = "child") -> dict[str, str]:
    """
    Build child environment by role:
      - 'orchestrator': keeps Git/GH auth for push/PR, still drops LINEAR_API_KEY (GraphQL uses urllib directly).
      - 'codex': keeps CODEX_SUB2API_API_KEY for model inference, strips Linear/GH env & isolates GH/Git helpers.
      - 'child': for agy/opencode/pi/verify/setup; strips Linear/GH/Codex keys & isolates GH/Git helpers.
    """
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    path_parts = env.get("PATH", "").split(":")
    if local_bin not in path_parts:
        env["PATH"] = f"{env.get('PATH', '')}:{local_bin}"

    if role == "orchestrator":
        env.pop("LINEAR_API_KEY", None)
        return env

    env["AGENT_DISPATCH_CHILD"] = "1"
    for var in ("LINEAR_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(var, None)
    for name in list(env):
        if name.startswith(("GIT_CONFIG_", "GIT_TRACE", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")) or name in (
            "GIT_SSH_COMMAND", "GIT_SSH", "GIT_CONFIG", "GIT_CURL_VERBOSE",
        ):
            env.pop(name)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if role != "codex":
        env.pop("CODEX_SUB2API_API_KEY", None)

    gh_empty, sanitized_git = ensure_sanitized_gitconfig()
    env["GH_CONFIG_DIR"] = str(gh_empty)
    env["GIT_CONFIG_GLOBAL"] = str(sanitized_git)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def find_harness_binary(name: str) -> str | None:
    path = build_child_env(role="orchestrator")["PATH"]
    executable = shutil.which(name, path=path)
    if not executable and name in ("agy", "pi"):
        executable = shutil.which(f"/root/.local/bin/{name}")
    return executable


class BoundedBuffer:
    """Accumulate subprocess output in memory, bounding RAM to head + rolling tail when capped."""
    def __init__(self, cap: int | None) -> None:
        self.cap = cap
        self.head = bytearray()
        self.tail = bytearray()
        self.truncated = False
        self.head_limit = (cap // 4) if cap else 0
        self.tail_limit = (cap // 2) if cap else 0

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.cap is None:
            self.head.extend(chunk)
            return
        if not self.truncated:
            if len(self.head) + len(chunk) <= self.cap:
                self.head.extend(chunk)
                return
            combined = bytes(self.head) + chunk
            self.head = bytearray(combined[: self.head_limit])
            self.tail = bytearray(combined[-self.tail_limit :])
            self.truncated = True
            return
        self.tail.extend(chunk)
        if len(self.tail) > self.tail_limit * 2:
            del self.tail[: len(self.tail) - self.tail_limit]

    def decode(self, *, redacted: bool, encoding: str = "utf-8") -> str:
        if not self.truncated:
            return self.head.decode(encoding, errors="replace")
        if not redacted:
            # Raw protocol bytes at a truncation boundary can contain a credential fragment.
            return "...[truncated]..."
        head_s = self.head.decode("utf-8", errors="replace")
        tail_s = self.tail[-self.tail_limit :].decode("utf-8", errors="replace")
        if "\n" in head_s:
            head_s = head_s.rsplit("\n", 1)[0]
        if "\n" in tail_s:
            tail_s = tail_s.split("\n", 1)[1]
        return redact_secrets(head_s) + "\n...[truncated]...\n" + redact_secrets(tail_s)


class RedactedLog:
    """Buffer partial lines so a token split across pipe reads never reaches disk."""
    line_limit = 2 * 1024 * 1024

    def __init__(self, path: Path, stack: contextlib.ExitStack, capture: BoundedBuffer | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = stack.enter_context(path.open("w", encoding="utf-8"))
        self.capture = capture
        self.pending = bytearray()
        self.in_pem = False
        self.discarding = False
        self.discarding_json = False
        self.marker_tail = ""

    def _emit(self, line: str) -> None:
        self.file.write(line)
        if self.capture is not None:
            self.capture.append(line.encode("utf-8"))

    def _discard_fragment(self, part: bytes) -> None:
        text = self.marker_tail + part.decode("ascii", errors="replace")
        if self.discarding_json:
            # A literal newline after a PEM header starts a real block even in broken JSON.
            # Escaped newlines and the closing quote of a JSON string stay on the same line.
            if part.strip():
                self.in_pem = bool(re.search(config.PEM_START.pattern + r"\s*\Z", text))
            self.marker_tail = text[-128:]
            return
        # An omitted plain-text line can start/end a multiline PRIVATE KEY PEM across pipe reads.
        starts = list(config.PEM_START.finditer(text))
        if starts:
            self.in_pem = True
            after = starts[-1].end()
        else:
            after = 0
        if self.in_pem and config.PEM_END.search(text, after):
            self.in_pem = False
        self.marker_tail = text[-128:]

    def write(self, chunk: bytes) -> None:
        for part in chunk.splitlines(keepends=True) if chunk else [b""]:
            complete = part.endswith(b"\n") or not chunk
            if not self.discarding and len(self.pending) + len(part) > self.line_limit:
                self.discarding = True
                prefix = bytes(self.pending) or part
                self.discarding_json = (not self.in_pem) and prefix.lstrip().startswith(b"{")
                self._discard_fragment(bytes(self.pending))
                self.pending.clear()
                self._emit("...[oversized diagnostic line omitted]...\n")
            if self.discarding:
                self._discard_fragment(part)
            else:
                self.pending.extend(part)
            if complete:
                if self.pending:
                    self._write_line(self.pending.decode("utf-8", errors="replace"))
                    self.pending.clear()
                self.discarding = False
                self.discarding_json = False
                self.marker_tail = ""
        self.file.flush()

    def _write_line(self, line: str) -> None:
        if not self.in_pem:
            # Parse self-contained JSON dicts first so a truncated PEM header inside JSON
            # cannot poison multiline plain-text PEM state for subsequent log lines.
            try:
                event = json.loads(line) if line.lstrip().startswith("{") else None
            except ValueError:
                event = None
            if isinstance(event, dict):
                if event.get("type") in ("message_update", "item.updated"):
                    # Deltas can split the same credential across multiple JSON events.
                    event = {"type": event["type"]}
                self._emit(safe_json(event, ensure_ascii=False) + "\n")
                return
        if self.in_pem:
            match = config.PEM_END.search(line)
            if not match:
                return
            line = line[match.end():]
            self.in_pem = False
        starts = list(config.PEM_START.finditer(line))
        if starts and not config.PEM_END.search(line, starts[-1].end()):
            self.in_pem = True
        self._emit(redact_secrets(line))


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(0.3)
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _drain_logs(proc: subprocess.Popen[bytes], logs: dict[str, RedactedLog]) -> None:
    # Drain a bounded amount of diagnostics already in the pipes without blocking.
    for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        if name in logs and pipe is not None and not pipe.closed:
            try:
                fd = pipe.fileno()
                os.set_blocking(fd, False)
                drained = 0
                while drained < 1024 * 1024:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    logs[name].write(chunk)
                    drained += len(chunk)
            except (BlockingIOError, OSError, ValueError):
                pass
            logs[name].write(b"")


def _stream_process(
    proc: subprocess.Popen[bytes],
    stack: contextlib.ExitStack,
    buffers: dict[str, BoundedBuffer],
    logs: dict[str, RedactedLog],
    stdin_text: str | None,
    timeout: int,
    output_limit: int | None,
    ignore_shutdown: bool,
) -> None:
    selector = stack.enter_context(selectors.DefaultSelector())
    assert proc.stdout is not None and proc.stderr is not None
    for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        stack.enter_context(pipe)
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    pending_input = memoryview((stdin_text or "").encode("utf-8"))
    if proc.stdin is not None:
        stack.enter_context(proc.stdin)
        if pending_input:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            proc.stdin.close()
    deadline = time.monotonic() + timeout
    while selector.get_map() or proc.poll() is None:
        if not ignore_shutdown:
            check_shutdown()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(redact_secrets(str(proc.args)), timeout)
        interval = min(remaining, 0.1)
        if not selector.get_map():
            try:
                proc.wait(timeout=interval)
            except subprocess.TimeoutExpired:
                pass
            continue
        for key, _ in selector.select(interval):
            if key.data == "stdin":
                try:
                    written = os.write(key.fd, pending_input[:4096])
                    pending_input = pending_input[written:]
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError):
                    pending_input = memoryview(b"")
                if not pending_input:
                    selector.unregister(key.fd)
                    key.fileobj.close()
                continue
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if output_limit is not None:
                output_limit -= len(chunk)
                if output_limit < 0:
                    raise RuntimeError("Harness output limit exceeded before a complete result was accepted.")
            sink = logs.get(key.data)
            if sink is None or sink.capture is None:
                buffers[key.data].append(chunk)
            if sink is not None:
                sink.write(chunk)
            if not chunk:
                selector.unregister(key.fd)
    if not ignore_shutdown:
        check_shutdown()


def run_cmd(
    cmd: list[str] | str,
    *,
    cwd: Path | None = None,
    stdin_text: str | None = None,
    timeout: int = 1800,
    shell: bool = False,
    check: bool = False,
    env_role: str = "child",
    extra_env: dict[str, str] | None = None,
    cap_output: bool = True,
    output_limit: int | None = None,
    stdout_encoding: str = "utf-8",
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    ignore_shutdown: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run subprocess in its own process group; UTF-8 replacement safe, shutdown-aware, streaming to disk."""
    if not ignore_shutdown:
        check_shutdown()
    env = build_child_env(role=env_role)
    if extra_env:
        env.update(extra_env)

    with contextlib.ExitStack() as stack:
        cap_bytes = config.DISPLAY_OUTPUT_CAP if cap_output else None
        buffers = {name: BoundedBuffer(cap_bytes) for name in ("stdout", "stderr")}
        logs = {name: RedactedLog(path, stack, buffers[name] if cap_output else None)
                for name, path in (("stdout", stdout_path), ("stderr", stderr_path)) if path is not None}
        proc = subprocess.Popen(
            cmd, cwd=cwd, shell=shell, env=env, start_new_session=True,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        register_active_pgid(proc.pid)
        try:
            _stream_process(proc, stack, buffers, logs, stdin_text, timeout, output_limit, ignore_shutdown)
        except BaseException:
            _terminate_process(proc)
            _drain_logs(proc, logs)
            raise
        finally:
            unregister_active_pgid(proc.pid)
        stdout = buffers["stdout"].decode(redacted="stdout" in logs, encoding=stdout_encoding)
        stderr = buffers["stderr"].decode(redacted="stderr" in logs)

    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {redact_secrets(str(cmd))}\n"
            f"STDOUT:\n{redact_secrets(result.stdout)[-2000:]}\n"
            f"STDERR:\n{redact_secrets(result.stderr)[-2000:]}"
        )
    return result
