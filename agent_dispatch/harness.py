"""Harness selection, headless execution, and protocol validation."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from agent_dispatch import config
from agent_dispatch.process import atomic_write_text, find_harness_binary, log, run_cmd
from agent_dispatch.secrets import redact_data, redact_secrets, safe_json


@dataclass(frozen=True)
class HarnessSpec:
    harness: str
    model: str
    effort: str

    @property
    def label(self) -> str:
        return f"{self.harness}:{self.model}@{self.effort}" if self.effort else f"{self.harness}:{self.model}"

    @property
    def vendor(self) -> str:
        m = self.model.lower()
        if "gemini" in m or self.harness == "agy":
            return "google"
        if "gpt" in m or self.harness == "codex":
            return "openai"
        return self.harness


def parse_harness_spec(raw: str) -> HarnessSpec:
    raw = raw.strip()
    effort = ""
    if "@" in raw:
        raw, effort = raw.rsplit("@", 1)
        effort = effort.strip().lower()
    if ":" in raw:
        harness, model_key = raw.split(":", 1)
    else:
        harness, model_key = raw, ""
    harness = harness.lower().strip()
    model_key = model_key.strip()
    if harness not in config.HARNESS_ALIASES:
        raise ValueError(f"Unsupported harness '{harness}'. Choose from: {', '.join(config.HARNESS_ALIASES)}")
    model = config.HARNESS_ALIASES[harness].get(model_key, model_key)

    if harness == "agy":
        if model not in config.HARNESS_ALIASES[harness].values():
            raise ValueError("Unsupported Gemini 4 model or tier")
        if effort:
            base = re.sub(r"-(low|medium|high|xhigh)$", "", model)
            tier = "medium" if effort == "med" else effort
            model = f"{base}-{tier}"
            effort = ""
            if model not in config.HARNESS_ALIASES[harness].values():
                raise ValueError("Unsupported Gemini 4 model or tier")
    else:
        if harness in ("opencode", "pi") and "/" not in model:
            model = f"sub2api/{model}"
        if model not in config.HARNESS_ALIASES[harness].values():
            raise ValueError("Unsupported GPT model for this harness")
        if effort and effort not in ("low", "medium", "high", "xhigh"):
            raise ValueError("Unsupported GPT reasoning effort")
        if not effort:
            effort = "xhigh"
    return HarnessSpec(harness=harness, model=model, effort=effort)


def default_reviewer_for(worker: HarnessSpec) -> HarnessSpec:
    if worker.vendor == "openai":
        return parse_harness_spec("agy:neon")
    return parse_harness_spec("codex:sol@xhigh")


def extract_json_object(text: str) -> dict[str, Any]:
    """Select the last JSON object in prose, including fenced output."""
    start = re.compile(r'\{(?=\s*(?:"|\}|\Z))|\[(?=\s*(?:[{"\[\]]|\Z))')
    tokens = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]"]', re.S)
    decoder = json.JSONDecoder()
    last = None
    pos = 0
    while match := start.search(text, pos):
        try:
            last, pos = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            # Skip a complete malformed draft as one object. Its children cannot be verdicts.
            last = None
            depth = 1
            for token in tokens.finditer(text, match.end()):
                value = token.group()
                if value in ("{", "["):
                    depth += 1
                elif value in ("}", "]"):
                    depth -= 1
                    if depth == 0:
                        pos = token.end()
                        break
                elif value == '"':
                    raise ValueError("Incomplete JSON string in final object")
            else:
                raise ValueError("Incomplete final JSON object")
    if not isinstance(last, dict):
        raise ValueError("Missing or invalid final JSON object")
    return last


def validate_schema(obj: Any, schema: dict[str, Any], *, expected_axis: str | None = None) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object, got {type(obj).__name__}")
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in obj:
            raise ValueError(f"Missing required JSON field: '{key}'")
    if schema.get("additionalProperties") is False:
        extra = set(obj.keys()) - set(props.keys())
        if extra:
            raise ValueError(f"Unexpected JSON fields: {sorted(extra)}")
    for key, spec in props.items():
        if key not in obj:
            continue
        val = obj[key]
        t = spec.get("type")
        if t == "string":
            if not isinstance(val, str):
                raise ValueError(f"Field '{key}' must be string, got {type(val).__name__}")
            if "enum" in spec and val not in spec["enum"]:
                raise ValueError(f"Field '{key}' value '{val}' not in {spec['enum']}")
        elif t == "boolean":
            if not isinstance(val, bool):
                raise ValueError(f"Field '{key}' must be boolean, got {type(val).__name__}")
        elif t == "array":
            if not isinstance(val, list):
                raise ValueError(f"Field '{key}' must be array, got {type(val).__name__}")
            item_t = (spec.get("items") or {}).get("type")
            if item_t == "string" and not all(isinstance(x, str) for x in val):
                raise ValueError(f"Field '{key}' must contain only strings")
    if expected_axis is not None and obj.get("axis") != expected_axis:
        raise ValueError(f"Review axis mismatch: expected '{expected_axis}', got '{obj.get('axis')}'")
    return obj


def _harness_events(stdout: str) -> Iterator[dict[str, Any]]:
    for line in stdout.split("\n"):
        if line.lstrip().startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if re.search(r'"type"\s*:', line):
                    raise ValueError("Malformed harness protocol event") from exc
                continue
            if isinstance(event, dict):
                yield event


def parse_codex_ndjson(stdout: str) -> str:
    text = ""
    completed = False
    for event in _harness_events(stdout):
        kind = event.get("type")
        if kind in ("turn.failed", "error"):
            raise RuntimeError("Codex reported an unsuccessful turn")
        if kind == "turn.started":
            text, completed = "", False
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                msg_text = item.get("text", "")
                text = msg_text if isinstance(msg_text, str) else ""
        elif kind == "turn.completed":
            completed = True
    if not text or not completed:
        raise ValueError("Codex produced no completed final assistant message")
    return text


def parse_opencode_ndjson(stdout: str) -> str:
    current: list[str] = []
    last_id = ""
    finished = False
    for event in _harness_events(stdout):
        kind = event.get("type")
        raw_part = event.get("part")
        part = raw_part if isinstance(raw_part, dict) else {}
        msg_id = str(part.get("messageID") or event.get("messageID") or "")
        if kind == "error":
            raise RuntimeError("OpenCode reported an error event")
        if kind == "step_start" or (msg_id and last_id and msg_id != last_id):
            current.clear()
            finished = False
        if msg_id:
            last_id = msg_id
        if kind == "text":
            text = part.get("text", event.get("text"))
            if isinstance(text, str):
                current.append(text)
        elif kind == "step_finish":
            finished = part.get("reason") == "stop"
    if not finished or not any(text.strip() for text in current):
        raise ValueError("OpenCode produced no completed final assistant message")
    return "\n".join(current)


def parse_pi_ndjson(stdout: str) -> str:
    final = None
    for event in _harness_events(stdout):
        kind = event.get("type")
        if kind == "error":
            raise RuntimeError("Pi reported an error event")
        msg = event.get("message")
        if kind in ("agent_start", "turn_start") or event.get("willRetry"):
            final = None
        elif kind in ("message_start", "message_update", "message_end") and isinstance(msg, dict) and msg.get("role") == "assistant":
            final = msg if kind == "message_end" else None
        elif kind == "agent_end":
            msgs = event.get("messages")
            final = next((m for m in reversed(msgs if isinstance(msgs, list) else [])
                          if isinstance(m, dict) and m.get("role") == "assistant"), None)
    if final and final.get("stopReason") in (None, "stop"):
        content = final.get("content")
        if isinstance(content, list):
            content = "\n".join(item["text"] for item in content if isinstance(item, dict)
                                and item.get("type") == "text" and isinstance(item.get("text"), str))
        if isinstance(content, str) and content.strip():
            return content
    raise ValueError("Pi produced no completed final assistant message")


_TRACE_WRITE_LOCK = threading.Lock()
_SESSION_PATTERNS = {
    "codex": r'(?:\bsession id:\s*|"thread_id"\s*:\s*")([0-9a-fA-F-]+)',
    "agy": r'"conversation_id"\s*:\s*"([0-9a-fA-F-]+)"',
    "opencode": r'"sessionID"\s*:\s*"([A-Za-z0-9_-]+)"',
}


def append_session_trace(run_dir: Path, entry: dict[str, Any], trace_sink: list[dict[str, Any]] | None) -> None:
    """Persist every session trace immediately to run_dir/sessions.jsonl so timeouts/crashes never lose traces."""
    entry = redact_data({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry})
    with _TRACE_WRITE_LOCK:
        trace_file = run_dir / "sessions.jsonl"
        trace_file.parent.mkdir(parents=True, exist_ok=True)
        with trace_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
        if trace_sink is not None and entry.get("event") == "end":
            trace_sink.append(entry)


@contextlib.contextmanager
def _pi_session_archive(path: Path, session_id: str, worktree: Path) -> Iterator[str]:
    """Let Pi append native session entries in memory and retain a redacted replay."""
    header = {"type": "session", "version": 3, "id": session_id, "cwd": str(worktree),
              "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with os.fdopen(os.memfd_create("agent-dispatch-pi"), "w+", encoding="utf-8") as session:
        session.write(json.dumps(header) + "\n")
        session.flush()
        try:
            yield f"/proc/{os.getpid()}/fd/{session.fileno()}"
        finally:
            try:
                session.seek(0)
                entries = []
                for line in session:
                    if not line.endswith("\n") or not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    entries.append(safe_json(parsed, ensure_ascii=False))
                if entries:
                    atomic_write_text(path, "\n".join(entries) + "\n")
            except Exception as exc:
                log(f"Warning: failed persisting redacted Pi session archive {path}: {exc}")


@dataclass(frozen=True)
class _HarnessRun:
    spec: HarnessSpec
    worktree: Path
    run_dir: Path
    role_name: str
    read_only: bool
    timeout: int
    stdout_log: Path
    stderr_log: Path
    trace: dict[str, Any]

    def _run(
        self, cmd: list[str], *, stdin_text: str | None = None, env_role: str = "child"
    ) -> subprocess.CompletedProcess[str]:
        return run_cmd(
            cmd,
            cwd=self.worktree,
            stdin_text=stdin_text,
            timeout=self.timeout,
            env_role=env_role,
            cap_output=False,
            output_limit=config.HARNESS_OUTPUT_LIMIT,
            stdout_path=self.stdout_log,
            stderr_path=self.stderr_log,
        )

    def codex(self, executable: str, prompt_text: str, schema_file: Path) -> Any:
        sandbox = "read-only" if self.read_only else "danger-full-access"
        wt_escaped = str(self.worktree).replace('"', '\\"')
        cmd = [
            executable,
            "exec",
            "-C",
            str(self.worktree),
            "-m",
            self.spec.model,
            "-c",
            f'model_reasoning_effort="{self.spec.effort}"',
            "-c",
            'approval_policy="never"',
            "-c",
            f'projects."{wt_escaped}".trust_level="trusted"',
            "-c",
            "mcp_servers.linear.enabled=false",
            "--sandbox",
            sandbox,
            "--output-schema",
            str(schema_file),
            "--json",
            "-",
        ]
        res = self._run(cmd, stdin_text=prompt_text, env_role="codex")
        sid_m = re.search(_SESSION_PATTERNS["codex"], res.stdout + "\n" + res.stderr)
        sid = sid_m.group(1) if sid_m else ""
        self.trace.update(
            {
                "returncode": res.returncode,
                "session_id": sid,
                "resume_cmd": f"codex resume {sid}" if sid else "codex agents",
            }
        )
        if res.returncode != 0:
            raise RuntimeError(f"codex exec exited {res.returncode}: {redact_secrets(res.stderr)[-2000:]}")
        return json.loads(parse_codex_ndjson(res.stdout))

    def agy(self, executable: str, prompt_file: Path, schema_file: Path) -> dict[str, Any]:
        short_prompt = (
            f"Read and execute all instructions in {prompt_file} (read-only reference). "
            f"Do NOT modify {prompt_file}. Return ONLY valid JSON matching the required schema."
        )
        cmd = [
            executable,
            "--model",
            self.spec.model,
            "--add-dir",
            str(self.run_dir),
            "--output-format",
            "json",
            "--json-schema",
            str(schema_file),
        ]
        if self.read_only:
            # Keep slash expansion enabled ONLY so --mode plan actually takes effect (prompt has no leading /cmd)
            cmd += ["--mode", "plan"]
        else:
            cmd += ["--dangerously-skip-permissions", "--disable-slash-commands"]
        cmd.append(f"--print={short_prompt}")

        res = self._run(cmd)
        # Extract conversation_id BEFORE checking returncode so failed runs remain traceable!
        wrapper: dict[str, Any] = {}
        try:
            wrapper = extract_json_object(res.stdout)
        except Exception:
            cid_m = re.search(_SESSION_PATTERNS["agy"], res.stdout + "\n" + res.stderr)
            if cid_m:
                wrapper = {"conversation_id": cid_m.group(1)}
        cid = str(wrapper.get("conversation_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9-]+", cid):
            cid = ""
        self.trace.update(
            {
                "returncode": res.returncode,
                "conversation_id": cid,
                "conversation_link": f"conversation://{cid}" if cid else "",
                "resume_cmd": f"agy --conversation {cid}" if cid else "",
                "duration_seconds": wrapper.get("duration_seconds"),
                "usage": wrapper.get("usage"),
            }
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"agy exited {res.returncode}: {redact_secrets(res.stderr)[-2000:]}\n{redact_secrets(res.stdout)[-1000:]}"
            )
        if wrapper.get("status") not in (None, "SUCCESS"):
            raise RuntimeError(f"agy returned non-SUCCESS status: {wrapper.get('status')}")
        result = wrapper.get("structured_output")
        if not isinstance(result, dict):
            result = extract_json_object(str(wrapper.get("response", "")))
        return result

    def opencode(
        self, executable: str, prompt_file: Path, schema: dict[str, Any], attach_files: list[Path] | None
    ) -> dict[str, Any]:
        files_to_attach: list[Path] = [prompt_file]
        seen_attach: set[Path] = {prompt_file.resolve()}
        for extra in attach_files or []:
            if extra and extra.is_file():
                resolved_extra = extra.resolve()
                if resolved_extra not in seen_attach:
                    seen_attach.add(resolved_extra)
                    files_to_attach.append(extra)
        ref_clause = " and the attached reference files" if len(files_to_attach) > 1 else ""
        short_prompt = (
            f"Read and execute all instructions in the attached prompt file ({prompt_file.name}){ref_clause}. "
            f"End your final message with ONLY a valid JSON object matching:\n{json.dumps(schema)}"
        )
        cmd = [
            executable,
            "run",
            "-m",
            self.spec.model,
            "--variant",
            self.spec.effort,
            "--dir",
            str(self.worktree),
            "--format",
            "json",
            "--title",
            self.role_name,
        ]
        if self.read_only:
            cmd += ["--agent", "plan"]
        else:
            cmd += ["--auto"]
        for af in files_to_attach:
            cmd += ["-f", str(af)]
        # `--` terminates yargs `-f [array]` so the positional message is never consumed as a filename
        cmd += ["--", short_prompt]
        res = self._run(cmd)
        sid_m = re.search(_SESSION_PATTERNS["opencode"], res.stdout)
        sid = sid_m.group(1) if sid_m else ""
        self.trace.update(
            {
                "returncode": res.returncode,
                "session_id": sid,
                "resume_cmd": f"opencode -s {sid}" if sid else "opencode session list",
            }
        )
        if res.returncode != 0:
            raise RuntimeError(f"opencode run exited {res.returncode}: {redact_secrets(res.stderr)[-2000:]}")
        return extract_json_object(parse_opencode_ndjson(res.stdout))

    def pi(
        self, executable: str, prompt_file: Path, schema: dict[str, Any], archive: Path, session_id: str
    ) -> dict[str, Any]:
        archive.parent.mkdir(parents=True, exist_ok=True)
        short_prompt = (
            f"Read and execute all instructions in {prompt_file}. "
            f"End your final message with ONLY a valid JSON object matching:\n{json.dumps(schema)}"
        )
        cmd = [
            executable,
            "-p",
            "--offline",
            "--mode",
            "json",
            "--name",
            self.role_name,
            "--session-dir",
            str(archive.parent),
            "--model",
            f"{self.spec.model}:{self.spec.effort}",
        ]
        if self.read_only:
            cmd += ["--tools", "read,grep,find,ls"]
        with _pi_session_archive(archive, session_id, self.worktree) as session_file:
            cmd += ["--session", session_file, short_prompt]
            res = self._run(cmd)
        self.trace["returncode"] = res.returncode
        if res.returncode != 0:
            raise RuntimeError(f"pi exited {res.returncode}: {redact_secrets(res.stderr)[-2000:]}")
        return extract_json_object(parse_pi_ndjson(res.stdout))

    def capture_partial_session(self) -> None:
        # Best-effort capture of partial session ID from streamed logs on crash/timeout
        if self.trace["status"] != "ok" and not self.trace.get("conversation_id") and not self.trace.get("session_id"):
            pattern = _SESSION_PATTERNS.get(self.spec.harness)
            for path in (self.stderr_log, self.stdout_log):
                if not pattern or not path.is_file():
                    continue
                with path.open(encoding="utf-8", errors="replace") as log_file:
                    match = next((m for line in log_file if (m := re.search(pattern, line))), None)
                if match:
                    session_id = match.group(1)
                    if self.spec.harness == "agy":
                        self.trace.update(conversation_id=session_id, conversation_link=f"conversation://{session_id}",
                                         resume_cmd=f"agy --conversation {session_id}")
                    else:
                        command = "codex resume" if self.spec.harness == "codex" else "opencode -s"
                        self.trace.update(session_id=session_id, resume_cmd=f"{command} {session_id}")
                    break


def invoke_harness(
    spec: HarnessSpec,
    prompt_text: str,
    *,
    worktree: Path,
    run_dir: Path,
    schema: dict[str, Any],
    schema_file: Path,
    prompt_file: Path,
    out_file: Path,
    read_only: bool,
    expected_axis: str | None = None,
    session_label: str = "",
    trace_sink: list[dict[str, Any]] | None = None,
    attach_files: list[Path] | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(schema_file, json.dumps(schema, indent=2))
    prompt_text = redact_secrets(prompt_text)
    atomic_write_text(prompt_file, prompt_text)
    if out_file.exists():
        out_file.unlink()

    role_name = session_label or out_file.stem
    stdout_log = run_dir / f"{out_file.stem}.stdout.log"
    stderr_log = run_dir / f"{out_file.stem}.stderr.log"

    pi_session_dir = run_dir / "pi-sessions"
    pi_session_id = str(uuid.uuid4()) if spec.harness == "pi" else ""
    pi_archive = pi_session_dir / f"{pi_session_id}.jsonl"

    append_session_trace(
        run_dir,
        {
            "event": "start",
            "role": role_name,
            "harness": spec.harness,
            "model": spec.label,
            "read_only": read_only,
            **({"session_id": pi_session_id} if pi_session_id else {}),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
        },
        trace_sink=None,
    )

    trace_end: dict[str, Any] = {
        "event": "end",
        "role": role_name,
        "harness": spec.harness,
        "model": spec.label,
        "read_only": read_only,
        "status": "error",
        **(
            {"session_id": pi_session_id, "resume_cmd": shlex.join(["pi", "--session", str(pi_archive)])}
            if pi_session_id
            else {}
        ),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "output_file": str(out_file),
    }

    run = _HarnessRun(spec, worktree, run_dir, role_name, read_only, timeout, stdout_log, stderr_log, trace_end)
    try:
        executable = find_harness_binary(spec.harness) or spec.harness
        if spec.harness == "codex":
            result = run.codex(executable, prompt_text, schema_file)
        elif spec.harness == "agy":
            result = run.agy(executable, prompt_file, schema_file)
        elif spec.harness == "opencode":
            result = run.opencode(executable, prompt_file, schema, attach_files)
        elif spec.harness == "pi":
            result = run.pi(executable, prompt_file, schema, pi_archive, pi_session_id)
        else:
            raise ValueError(f"Unknown harness: {spec.harness}")
        validated = redact_data(validate_schema(result, schema, expected_axis=expected_axis))
        atomic_write_text(out_file, json.dumps(validated, indent=2, ensure_ascii=False))
        trace_end["status"] = "ok"
        return validated
    except subprocess.TimeoutExpired:
        trace_end["status"] = "timeout"
        raise
    finally:
        run.capture_partial_session()
        append_session_trace(run_dir, trace_end, trace_sink)
