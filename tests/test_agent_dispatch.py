#!/usr/bin/env python3
"""
Unit, security/false-positive, end-to-end CLI fault-injection, parallel frontier, real-signal,
and opt-in live Linear contract tests for agent-dispatch v5 and install.sh.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

# Prevent SourceFileLoader from leaving .pyc artifacts inside bin/__pycache__
sys.dont_write_bytecode = True

WORKFLOW_ROOT = Path(__file__).resolve().parent.parent
BIN_PATH = WORKFLOW_ROOT / "bin" / "agent-dispatch"
INSTALL_SH = WORKFLOW_ROOT / "install.sh"

loader = importlib.machinery.SourceFileLoader("agent_dispatch", str(BIN_PATH))
spec = importlib.util.spec_from_loader("agent_dispatch", loader)
assert spec is not None
ad = importlib.util.module_from_spec(spec)
sys.modules["agent_dispatch"] = ad
loader.exec_module(ad)


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=str(cwd), text=True, capture_output=True, check=check).stdout.strip()


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Local HTTP FakeLinear Server for True Subprocess E2E Fault Injection
# ---------------------------------------------------------------------------

class FakeLinearState:
    def __init__(self, project_name: str = "demo-repo") -> None:
        self.project = {"id": "proj-1", "name": project_name, "state": "started", "teams": {"nodes": [{"id": "team-1", "key": "TST", "name": "Test"}]}}
        self.states = [
            {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
            {"id": "st-todo", "name": "Todo", "type": "unstarted"},
            {"id": "st-prog", "name": "In Progress", "type": "started"},
            {"id": "st-rev", "name": "In Review", "type": "started"},
            {"id": "st-done", "name": "Done", "type": "completed"},
            {"id": "st-canceled", "name": "Canceled", "type": "canceled"},
        ]
        self.team_labels: list[dict[str, str]] = [
            {"id": "lbl-ready", "name": "agent-ready"},
        ]
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.fail_next_comment_create = False
        self.fail_next_state_update = False
        self.mutation_log: list[tuple[str, dict[str, Any]]] = []
        self._tick = 1

    def add_issue(
        self,
        identifier: str,
        title: str,
        description: str,
        *,
        state_name: str = "Todo",
        labels: list[str] | None = None,
        uuid_str: str | None = None,
    ) -> dict[str, Any]:
        st = next(s for s in self.states if s["name"] == state_name)
        uid = uuid_str or f"uuid-{identifier.lower()}"
        lbl_objs = []
        for ln in labels or []:
            ex = next((l for l in self.team_labels if l["name"] == ln), None)
            if not ex:
                ex = {"id": f"lbl-{len(self.team_labels)+1}", "name": ln}
                self.team_labels.append(ex)
            lbl_objs.append(ex)
        iss = {
            "id": uid,
            "identifier": identifier,
            "title": title,
            "description": description,
            "url": f"https://linear.app/tst/issue/{identifier}",
            "priority": 2,
            "updatedAt": f"2026-11-24T00:00:{self._tick:02d}Z",
            "assignee": None,
            "state": st,
            "team": {"id": "team-1", "key": "TST", "name": "Test"},
            "project": {"id": self.project["id"], "name": self.project["name"]},
            "parent": None,
            "_labels": lbl_objs,
            "_inverseRelations": [],
            "_children": [],
        }
        self._tick += 1
        self.issues[uid] = iss
        self.issues[identifier] = iss
        self.comments.setdefault(uid, [])
        return iss

    def get_issue(self, key: str) -> dict[str, Any] | None:
        return self.issues.get(key)

    def handle(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "teams(first:" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1", "key": "TST", "name": "Test"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        if "projects(first:" in query:
            return {"data": {"projects": {"nodes": [self.project], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        if "team(id: $id)" in query and "states(first:" in query:
            return {"data": {"team": {"states": {"nodes": self.states, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        if "team(id: $id)" in query and "labels(first:" in query:
            return {"data": {"team": {"labels": {"nodes": self.team_labels, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        if "project(id: $projectId)" in query:
            uniq = list({v["id"]: v for v in self.issues.values()}.values())
            st_type = variables.get("stateType")
            if st_type:
                uniq = [i for i in uniq if i["state"]["type"] == st_type]
            nodes = [
                {
                    **{k: v for k, v in i.items() if not k.startswith("_")},
                    "labels": {"nodes": i["_labels"], "pageInfo": {"hasNextPage": False}},
                    "children": {"nodes": i["_children"]},
                }
                for i in uniq
            ]
            return {"data": {"project": {"id": self.project["id"], "name": self.project["name"], "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        if "issueLabels(filter:" in query:
            return {"data": {"issueLabels": {"nodes": []}}}
        if "mutation" in query and ("issueAddLabel(" in query or "issueRemoveLabel(" in query):
            iss = self.get_issue(variables["id"])
            label = next(l for l in self.team_labels if l["id"] == variables["labelId"])
            if "issueAddLabel(" in query:
                if label not in iss["_labels"]:
                    iss["_labels"].append(label)
                operation, event = "issueAddLabel", "addLabels"
            else:
                iss["_labels"] = [l for l in iss["_labels"] if l["id"] != label["id"]]
                operation, event = "issueRemoveLabel", "removeLabels"
            self.mutation_log.append((event, {"id": iss["identifier"], "labels": [l["name"] for l in iss["_labels"]]}))
            return {"data": {operation: {"success": True}}}
        if "mutation" in query and "issueLabelCreate" in query:
            name = variables["name"]
            ex = next((l for l in self.team_labels if l["name"].lower() == name.lower()), None)
            if not ex:
                ex = {"id": f"lbl-{len(self.team_labels)+1}", "name": name}
                self.team_labels.append(ex)
            self.mutation_log.append(("issueLabelCreate", {"name": name}))
            return {"data": {"issueLabelCreate": {"success": True, "issueLabel": ex}}}
        if "mutation" in query and "issueUpdate" in query:
            iss = self.get_issue(variables["id"])
            assert iss is not None
            if "stateId" in variables:
                if self.fail_next_state_update:
                    self.fail_next_state_update = False
                    return {"data": {"issueUpdate": {"success": False, "issue": None}}}
                st = next(s for s in self.states if s["id"] == variables["stateId"])
                iss["state"] = st
                self._tick += 1
                iss["updatedAt"] = f"2026-11-24T00:00:{self._tick:02d}Z"
                self.mutation_log.append(("state", {"id": iss["identifier"], "state": st["name"]}))
                return {"data": {"issueUpdate": {"success": True, "issue": {"id": iss["id"], "identifier": iss["identifier"], "updatedAt": iss["updatedAt"], "state": st}}}}
        if "mutation" in query and "commentCreate" in query:
            if self.fail_next_comment_create:
                self.fail_next_comment_create = False
                return {"errors": [{"message": "Simulated transient Linear comment failure"}]}
            uid = variables["issueId"]
            cid = f"cmt-{len(self.comments.setdefault(uid, []))+1}"
            self.comments[uid].append({"id": cid, "body": variables["body"], "createdAt": f"2026-11-24T00:01:{len(self.comments[uid]):02d}Z", "user": {"name": "bot"}})
            self.mutation_log.append(("commentCreate", {"issueId": uid}))
            return {"data": {"commentCreate": {"success": True, "comment": {"id": cid}}}}
        if "issue(id: $id)" in query:
            iss = self.get_issue(variables["id"])
            if not iss:
                return {"data": {"issue": None}}
            if "labels(first:" in query:
                return {"data": {"issue": {"labels": {"nodes": iss["_labels"], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
            if "comments(first:" in query:
                return {"data": {"issue": {"comments": {"nodes": self.comments[iss["id"]], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
            if "inverseRelations(first:" in query:
                return {"data": {"issue": {"inverseRelations": {"nodes": iss["_inverseRelations"], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
            if "children(first:" in query:
                return {"data": {"issue": {"children": {"nodes": iss["_children"], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
            if "{ updatedAt }" in query:
                return {"data": {"issue": {"updatedAt": iss["updatedAt"]}}}
            return {"data": {"issue": {k: v for k, v in iss.items() if not k.startswith("_")}}}
        return {"errors": [{"message": f"Unhandled fake query: {query[:120]}"}]}


def start_fake_linear(state: FakeLinearState) -> tuple[http.server.ThreadingHTTPServer, str]:
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            with lock:
                resp = state.handle(body.get("query", ""), body.get("variables") or {})
                data = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}/graphql"


# ---------------------------------------------------------------------------
# Unit Tests: Schemas, Regexes, False Positives, Gates & Touches
# ---------------------------------------------------------------------------

class IsolatedDispatchCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        sandbox = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(sandbox),
            "PATH": os.defpath + ":/usr/local/bin",
            "LC_ALL": "C.UTF-8",
            "AGENT_DISPATCH_CACHE_ROOT": str(sandbox / "cache"),
            "AGENT_DISPATCH_LINEAR_URL": "http://127.0.0.1:1/graphql",
            "LINEAR_API_KEY": "synthetic-test-credential",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=True))
        self.enterContext(mock.patch.object(ad, "MCP_CONFIG_PATH", sandbox / "unused-mcp.json"))


class TestUnitGatesAndParsing(IsolatedDispatchCase):
    def test_harness_model_policy_rejects_unlisted_names_and_tiers(self) -> None:
        for spec in ("agy:opus", "agy:claude-sonnet", "agy:gemini-3", "agy:neon@bogus",
                     "codex:6.1-sol", "opencode:sub2api/custom", "pi:other/gpt-6.2-astra"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                ad.parse_harness_spec(spec)
        for harness in ("codex", "opencode", "pi"):
            for model in ("gpt-6.2-sol", "gpt-6.2-astra"):
                self.assertTrue(ad.parse_harness_spec(f"{harness}:{model}").model.endswith(model))
        for model in ("neon", "carbon", "argon", "hydrogen"):
            self.assertTrue(ad.parse_harness_spec(f"agy:{model}").model.startswith("gemini-4-"))

    def test_ndjson_incomplete_final_turn_cannot_reuse_prior_verdict(self) -> None:
        text = '{"verdict":"pass"}'
        previous = {"role": "assistant", "content": text, "stopReason": "stop"}
        failed = {"role": "assistant", "content": [], "stopReason": "error"}
        for final in ({"type": "message_update", "message": previous},
                      {"type": "message_end", "message": failed},
                      {"type": "agent_end", "messages": [previous, failed]}):
            with self.subTest(event=final["type"]), self.assertRaises(ValueError):
                ad.parse_pi_ndjson("\n".join(map(json.dumps, [{"type": "message_end", "message": previous}, final])))
        oc = [{"type": "text", "part": {"text": text}}, {"type": "step_finish", "part": {"reason": "stop"}},
              {"type": "step_start"}, {"type": "step_finish", "part": {"reason": "length"}}]
        with self.assertRaises(ValueError):
            ad.parse_opencode_ndjson("\n".join(map(json.dumps, oc)))
        cx = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
              {"type": "turn.completed"}, {"type": "turn.started"}]
        with self.assertRaises(ValueError):
            ad.parse_codex_ndjson("\n".join(map(json.dumps, cx)))

    def test_streamed_logs_redact_credentials_before_persistence_and_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"UNIT_API_KEY": "synthetic-opaque-value"}):
            d = Path(td)
            payload = ("Bearer invented-only-example\n" + os.environ["UNIT_API_KEY"] + "\n"
                       + "-----BEGIN " + "PRIVATE KEY-----\n" + "example-key-body\n"
                       + "-----END " + "PRIVATE KEY-----\n")
            code = ("import os,sys,time\nfor line in sys.stdin.buffer:\n"
                    " for fd in (1,2):\n  os.write(fd,line[:5]); os.write(fd,line[5:])\n")
            for timeout in (False, True):
                args = [sys.executable, "-B", "-c", code + ("time.sleep(10)" if timeout else "")]
                kw = dict(stdin_text=payload, cwd=d, stdout_path=d / "out", stderr_path=d / "err",
                          timeout=0.5 if timeout else 5)
                if timeout:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        ad.run_cmd(args, **kw)
                else:
                    ad.run_cmd(args, **kw)
                for file in (d / "out", d / "err"):
                    body = file.read_text()
                    self.assertIn("<REDACTED_SECRET>", body)
                    for secret in ("invented-only-example", "example-key-body", os.environ["UNIT_API_KEY"]):
                        self.assertNotIn(secret, body)

    def test_review_prompt_result_trace_and_snapshots_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            secret = "lin_api_" + "syntheticvalue123456"
            output = {"status": "completed", "summary": secret, "tests_run": [], "discovered_issues": [], "blockers": ""}
            def codex_response(cmd, **kw):
                self.assertNotIn(secret, kw["stdin_text"])
                self.assertIn("--json", cmd)
                self.assertNotIn("-o", cmd)
                events = [{"type": "thread.started", "thread_id": "aaaa-bbbb"},
                          {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(output)}},
                          {"type": "turn.completed"}]
                return subprocess.CompletedProcess(cmd, 0, "\n".join(map(json.dumps, events)), "")
            with mock.patch.object(ad, "run_cmd", side_effect=codex_response):
                result = ad.invoke_harness(ad.parse_harness_spec("codex"), secret, worktree=d, run_dir=d,
                                           schema=ad.WORKER_SCHEMA, schema_file=d / "schema", prompt_file=d / "prompt",
                                           out_file=d / "result", read_only=False, session_label=secret)
                self.assertEqual(result["summary"], "<REDACTED_SECRET>")
            for path in (d / "prompt", d / "result", d / "sessions.jsonl"):
                self.assertNotIn(secret, path.read_text())
            _git(d, "init", "-b", "main")
            (d / "GLOSSARY.md").write_text(secret)
            _git(d, "add", "GLOSSARY.md")
            _git(d, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "baseline")
            ad.snapshot_baseline_standards(d, "HEAD", d / "standards")
            self.assertNotIn(secret, (d / "standards" / "GLOSSARY.md").read_text())

    def test_child_git_config_cannot_inherit_auth_rewrites_or_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / ".gitconfig").write_text('[url "https://fictional-token@invalid.example/"]\n    insteadOf = local:!\n')
            with mock.patch.dict(os.environ, {"HOME": td, "AGENT_DISPATCH_CACHE_ROOT": str(d / "cache"),
                                               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_VALUE_0": "fictional-token",
                                               "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_TRACE": "1"}):
                env = ad.build_child_env()
            self.assertFalse({"GIT_CONFIG_COUNT", "GIT_CONFIG_VALUE_0", "GIT_TRACE"} & env.keys())
            self.assertEqual(Path(env["GIT_CONFIG_GLOBAL"]).read_text(), "")
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_pi_session_replay_preserves_native_entries_and_redacts_on_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            archive = d / "session.jsonl"
            secret = "lin_api_" + "syntheticpioutput12345"
            entry = {"type": "message", "id": "a1", "parentId": None, "message": {"content": secret}}
            with self.assertRaises(KeyboardInterrupt):
                with ad._pi_session_archive(archive, "11111111-2222-3333-4444-555555555555", d) as native:
                    subprocess.run([sys.executable, "-B", "-c",
                                    "import sys; open(sys.argv[1], 'a').write(sys.stdin.read())", native],
                                   input=json.dumps(entry) + "\n" + '{"type":', text=True, check=True)
                    self.assertFalse(archive.exists())
                    raise KeyboardInterrupt
            header, message = map(json.loads, archive.read_text().splitlines())
            self.assertEqual(header["version"], 3)
            self.assertEqual(message["id"], "a1")
            self.assertEqual(message["message"]["content"], "<REDACTED_SECRET>")

    def test_harness_aliases_and_heterogeneous_reviewer(self) -> None:
        w_codex = ad.parse_harness_spec("codex:sol@xhigh")
        self.assertEqual(w_codex.label, "codex:gpt-6.2-sol@xhigh")
        self.assertEqual(ad.default_reviewer_for(w_codex).label, "agy:gemini-4-neon-xhigh")

        w_agy = ad.parse_harness_spec("agy:neon@high")
        self.assertEqual(w_agy.model, "gemini-4-neon-high")
        self.assertEqual(w_agy.effort, "")
        self.assertEqual(ad.default_reviewer_for(w_agy).label, "codex:gpt-6.2-sol@xhigh")

        w_oc = ad.parse_harness_spec("opencode:astra")
        self.assertEqual(w_oc.model, "sub2api/gpt-6.2-astra")
        self.assertEqual(ad.default_reviewer_for(w_oc).label, "agy:gemini-4-neon-xhigh")

        w_pi = ad.parse_harness_spec("pi:sol@high")
        self.assertEqual(w_pi.model, "sub2api/gpt-6.2-sol")
        self.assertEqual(w_pi.effort, "high")

    def test_extract_json_raw_decode_with_trailing_logs(self) -> None:
        raw = 'noise {"a": 1}\nfinal: {"status": "completed", "summary": "ok"} trailing log line\n'
        self.assertEqual(ad.extract_json_object(raw), {"status": "completed", "summary": "ok"})

    def test_pi_ndjson_ignores_user_schema_and_takes_last_assistant(self) -> None:
        events = [
            json.dumps({"type": "message_end", "message": {"role": "user", "content": json.dumps(ad.WORKER_SCHEMA)}}),
            json.dumps({"type": "message_end", "message": {"role": "toolResult", "content": '{"noise": true}'}}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": '{"status":"completed","summary":"done","tests_run":["pytest"],"discovered_issues":[],"blockers":""}'}]}}),
        ]
        txt = ad.parse_pi_ndjson("\n".join(events))
        obj = ad.validate_schema(ad.extract_json_object(txt), ad.WORKER_SCHEMA)
        self.assertEqual(obj["summary"], "done")

    def test_schema_validator_rejects_missing_and_wrong_axis(self) -> None:
        with self.assertRaises(ValueError):
            ad.validate_schema({"axis": "standards", "verdict": "pass"}, ad.REVIEW_SCHEMA, expected_axis="standards")
        valid_std = {
            "axis": "standards",
            "verdict": "pass",
            "hard_findings": [],
            "suggestions": [],
            "test_integrity_violation": False,
            "summary": "Clean.",
        }
        with self.assertRaises(ValueError):
            ad.validate_schema(valid_std, ad.REVIEW_SCHEMA, expected_axis="spec")
        self.assertEqual(ad.validate_schema(valid_std, ad.REVIEW_SCHEMA, expected_axis="standards")["verdict"], "pass")

    def test_adr_frontmatter_parser_and_pr_markdown_rendering(self) -> None:
        self.assertTrue(ad.is_active_adr("---\nstatus: accepted # accepted | deprecated | superseded by ADR-0002\n---\n# Ok"))
        self.assertFalse(ad.is_active_adr("---\nstatus: superseded by ADR-0005\n---\n# Old"))
        self.assertFalse(ad.is_active_adr("---\nstatus: deprecated\n---\n# Old"))

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

    def test_secret_patterns_no_false_positives_on_kebab_or_env_example(self) -> None:
        # Must NOT false-positive on kebab-case identifiers or .env.example!
        self.assertEqual(ad.redact_secrets("task-list-container-component"), "task-list-container-component")
        self.assertFalse(ad._is_secret_filename(".env.example"))
        self.assertFalse(ad._is_secret_filename("config/.env.sample"))
        self.assertTrue(ad._is_secret_filename(".env"))
        self.assertTrue(ad._is_secret_filename(".env.local"))
        self.assertTrue(ad._is_secret_filename("目录/.env"))
        # Must catch expanded real token families
        for sample in (
            "sk-proj-1234567890abcdefghij1234",
            "github_pat_11ABCDEFG01234567890_abcdefghijklmnop",
            "sk_live_1234567890abcdef12",
            "xoxb-1234567890-abcdef",
            "AIza" + "X" * 35,
            "glpat-1234567890abcdefghij",
        ):
            self.assertEqual(ad.redact_secrets(f"key={sample}"), "key=<REDACTED_SECRET>", f"Missed redaction for {sample}")
            self.assertTrue(ad.scan_text_for_secrets(f"Fix {sample}", "title"), f"Missed title secret for {sample}")

    def test_child_env_stripping_isolated_without_polluting_os_environ(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {
                    "AGENT_DISPATCH_CACHE_ROOT": td,
                    "LINEAR_API_KEY": "lin_api_test1234567890",
                    "GH_TOKEN": "gho_1234567890123456789012345",
                    "GITHUB_TOKEN": "ghp_1234567890123456789012345",
                    "SSH_AUTH_SOCK": "/tmp/fake-ssh.sock",
                    "CODEX_SUB2API_API_KEY": "sub2api_test_key",
                },
            ):
                child_env = ad.build_child_env(role="child")
                self.assertEqual(child_env["AGENT_DISPATCH_CHILD"], "1")
                self.assertEqual(child_env["GIT_TERMINAL_PROMPT"], "0")
                self.assertTrue(child_env["GH_CONFIG_DIR"].startswith(td))
                for k in ("LINEAR_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "CODEX_SUB2API_API_KEY"):
                    self.assertNotIn(k, child_env)
                codex_env = ad.build_child_env(role="codex")
                self.assertIn("CODEX_SUB2API_API_KEY", codex_env)
                self.assertNotIn("LINEAR_API_KEY", codex_env)
                self.assertNotIn("GH_TOKEN", codex_env)
                self.assertNotIn("SSH_AUTH_SOCK", codex_env)

    def test_authorization_requires_real_heading_and_matching_fence(self) -> None:
        brief = (
            "````md\n## Authorized Gate/Test Changes\n- gate: Makefile\n```\n"
            "- gate: pytest.ini\n````\n## Authorized Gate/Test Changes\n"
            "    - gate: setup.py\n~~~\n```\n- gate: setup.cfg\n~~~\n- gate: src/Makefile\n"
            "# Out of scope\n- gate: Cargo.toml\n"
        )
        self.assertEqual(ad.parse_authorized_changes(brief), (["src/Makefile"], []))

    def test_mixed_and_zero_collection_cannot_pass_verify(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            case = tests / "test_case.py"
            with mock.patch.object(ad.shutil, "which", return_value=None):
                for source in ("class TestPlain:\n    def test_fail(self): assert False\n",
                               "async def test_fail(): assert False\n"):
                    case.write_text(source)
                    self.assertIsNone(ad.detect_verify_command(root, None))
                case.write_text("from unittest import TestCase as Case\nclass Base(Case): pass\nclass TestApp(Base): pass\n")
                cmd = ad.detect_verify_command(root, None)
                self.assertIsNotNone(cmd)
                _git(root, "init", "-b", "main")
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(root / "cache")}):
                    # Python versions before 3.12 exit successfully after collecting zero tests.
                    result = ad.run_verify_gate(root, f"{cmd} || true", allow_no_verify=False, log_file=root / "cache/log",
                                                repo_key="test-zero")
                self.assertFalse(result["passed"])
                self.assertIn("zero tests", result["output"])
            case.unlink()
            (root / "pyproject.toml").touch()
            (root / "test_app.py").write_text("def test_fail(): assert False")
            with mock.patch.object(ad.shutil, "which", side_effect=lambda name: "/mock/ruff" if name == "ruff" else None):
                self.assertIsNone(ad.detect_verify_command(root, None))
            tests.rmdir()
            (root / "src").mkdir()
            (root / "src" / "thing.spec.tsx").touch()
            (root / "package.json").write_text('{"scripts":{"lint":"eslint ."}}')
            self.assertIsNone(ad.detect_verify_command(root, None))

    def test_git_secret_scan_covers_binary_added_text_names_and_intermediate_commits(self) -> None:
        token = "lin_api_" + "SYNTHETICEXAMPLE012345"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git(root, "init", "-b", "main")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            _git(root, "commit", "--allow-empty", "-m", "base")
            base = _git(root, "rev-parse", "HEAD")
            for name, content in [("asset.dat", b"\0 " + token.encode()), ("text", ("++ " + token).encode()),
                                  (token, b"ordinary")]:
                with self.subTest(file_kind="name" if name == token else name):
                    path = root / name
                    path.write_bytes(content)
                    _git(root, "add", "--", name)
                    hits = ad.scan_staged_for_secrets(root)
                    self.assertTrue(hits)
                    self.assertNotIn(token, str(hits))
                    _git(root, "commit", "-m", "introduce fixture")
                    head = _git(root, "rev-parse", "HEAD")
                    self.assertTrue(ad.scan_range_for_secrets(root, base, head))
                    path.unlink()
                    _git(root, "commit", "-am", "remove fixture")
                    cleaned = _git(root, "rev-parse", "HEAD")
                    self.assertFalse(ad.scan_range_for_secrets(root, head, cleaned))
                    self.assertTrue(ad.scan_range_for_secrets(root, base, cleaned))
                    base = cleaned

    def test_nested_protected_files_and_literal_test_pathspecs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git(root, "init", "-b", "main")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            _git(root, "commit", "--allow-empty", "-m", "base")
            files = {"packages/a/.cargo/config.toml": "", "packages/a/package.json": '{"scripts":{"test":"true"}}',
                     "packages/a/docs/adr/decision.md": "doc", "tests/test_[fixture].py": "@unittest.skip('reason')"}
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "candidate")
            protected = ad.check_protected_paths(root, "HEAD^", "HEAD")
            self.assertTrue(all(any(path in hit for hit in protected) for path in list(files)[:3]))
            self.assertTrue(ad.check_test_integrity_diff(root, "HEAD^", "HEAD"))

    def test_secret_edits_remain_in_place_without_new_git_objects_or_stash(self) -> None:
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
                _, hits = ad.orchestrator_commit(repo, "SYN-1", "local edits", 1)
                self.assertTrue(hits)
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
            with mock.patch.object(ad, "_SHUTDOWN_SIGNAL", signal.SIGHUP):
                ad._stash_dirty_worktree_for_safe_rerun(repo, "halt")
            self.assertIn("halt", _git(repo, "stash", "list"))
            self.assertEqual(_git(repo, "show", "stash^3:safe.txt"), "save me")

    def test_baseline_cache_distinguishes_setup_and_rejects_changed_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "commit", "--allow-empty", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "commit", "--allow-empty", "-m", "ahead")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(root / "cache")}):
                for setup, passed in [("touch marker", True), ("true", False)]:
                    result = ad.check_base_baseline(repo, "r", repo, base, "test -f marker", root / "run", setup_cmd_override=setup)
                    self.assertEqual(result["passed"], passed)
                    self.assertFalse(result["cached"])
                for verify in ("git commit --allow-empty -m change", "git switch -c other && false"):
                    with self.assertRaisesRegex(RuntimeError, "changed the worktree branch or HEAD"):
                        ad.check_base_baseline(repo, "r", repo, base, verify, root / "run")
                self.assertFalse(list((root / "cache/worktrees/r").glob(".baseline-*")))
                self.assertEqual(_git(repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_justfile_and_makefile_variable_assignments_not_misdetected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "Makefile").write_text("verify := echo fake\ncheck-all:\n\techo nope\n", encoding="utf-8")
            self.assertIsNone(ad.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\ncheck-all:\n    echo nope\n', encoding="utf-8")
            self.assertIsNone(ad.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\nverify:\n    pytest\n', encoding="utf-8")
            self.assertEqual(ad.detect_verify_command(d, None), "just verify")

    def test_linear_transient_reads_and_credential_redirect_policy(self) -> None:
        from io import BytesIO
        responses = [
            {"errors": [{"extensions": {"code": "RATELIMITED"}}]},
            {"data": {"ok": True}},
        ]
        def boundary(request, timeout):
            self.assertEqual(request.get_header("Authorization"), "synthetic-test-credential")
            self.assertNotIn("Authorization", request.headers)  # urllib only copies these headers on redirect
            return BytesIO(json.dumps(responses.pop(0)).encode())
        with mock.patch.object(ad.urllib.request, "urlopen", side_effect=boundary), mock.patch.object(ad.time, "sleep"):
            self.assertEqual(ad.LinearClient("synthetic-test-credential").query("query { ok }"), {"ok": True})
        self.assertFalse(responses)

    def test_linear_auth_normalizes_personal_key_and_oauth(self) -> None:
        key = "lin_api_" + "X" * 24
        for value, expected in [(key, key), ("Bearer " + key, key), ("test-oauth", "Bearer test-oauth")]:
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": value}):
                self.assertEqual(ad.resolve_linear_auth(), expected)

    def test_shutdown_interrupts_contended_file_lock(self) -> None:
        path = Path(os.environ["AGENT_DISPATCH_CACHE_ROOT"]) / "verify.lock"
        with ad.file_lock(path):
            with mock.patch.object(ad, "_SHUTDOWN_SIGNAL", signal.SIGTERM):
                with self.assertRaises(SystemExit):
                    with ad.file_lock(path):
                        self.fail("shutdown waiter acquired the lock")

    def test_label_pagination_and_concurrent_human_labels(self) -> None:
        client = ad.LinearClient("synthetic-test-credential")
        rows = [{"id": "one", "labels": {"nodes": [], "pageInfo": {"hasNextPage": True}}}]
        pages = [
            {"issue": {"labels": {"nodes": [{"id": "a", "name": "A"}], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}},
            {"issue": {"labels": {"nodes": [{"id": "b", "name": "B"}], "pageInfo": {"hasNextPage": False}}}},
        ]
        with mock.patch.object(client, "query", side_effect=pages) as query:
            client._complete_issue_labels(rows)
            self.assertEqual([x["id"] for x in rows[0]["labels"]["nodes"]], ["a", "b"])
            self.assertEqual(query.call_args.args[1]["after"], "next")
        labels = {"human-A"}
        def boundary(query, variables, **_):
            if "issueAddLabel(" in query:
                labels.add("concurrent-human")
                labels.add(variables["labelId"])
                return {"issueAddLabel": {"success": True}}
            if "issueRemoveLabel(" in query:
                labels.remove(variables["labelId"])
                return {"issueRemoveLabel": {"success": True}}
            if "labels(first:" in query:
                return {"issue": {"labels": {"nodes": [{"id": x, "name": x} for x in labels], "pageInfo": {"hasNextPage": False}}}}
            self.fail("unexpected label operation")
        issue = {"id": "one", "team": {"labels": {"nodes": [{"id": "dispatch:failed", "name": "dispatch:failed"}]}}}
        with mock.patch.object(client, "query", side_effect=boundary):
            client.ensure_label_and_add(issue, "dispatch:failed")
            client.remove_labels_if_present(issue, {"dispatch:failed"})
        self.assertEqual(labels, {"human-A", "concurrent-human"})

    def test_touches_wave_selection_frontier_label_and_fail_closed_authorization(self) -> None:
        self.assertEqual(ad.parse_touches("## Touches\n- n/a\n"), set())
        self.assertEqual(ad.parse_touches("## Touches\n- src/routes, auth\n"), {"src/routes", "auth"})

        # Fail-closed authorization parser: ALL free-form/none prose returns ([], [])!
        for prose in (
            "- None",
            "- None (list explicitly only if authorized)",
            "- Not applicable",
            "- No changes authorized",
            "- N/A (pure feature)",
            "- (none)",
            "- _None_",
            "- 无",
            "- Migrate pytest.ini",  # Unstructured -> rejected fail-closed!
            "- gate: *",  # Wildcard -> rejected!
        ):
            self.assertEqual(
                ad.parse_authorized_changes(f"## Authorized Gate/Test Changes\n{prose}\n"),
                ([], []),
                f"Expected fail-closed empty auth for {prose!r}",
            )
        self.assertEqual(
            ad.parse_authorized_changes("## Authorized Gate/Test Changes\n- gate: `pytest.ini`\n- test: tests/legacy_*.py\n"),
            (["pytest.ini"], ["tests/legacy_*.py"]),
        )

        # Frontier MUST require `agent-ready` label even if ## Acceptance Criteria exists!
        unlabeled = {
            "identifier": "TST-H1",
            "title": "Human ticket",
            "description": "## Acceptance Criteria\n- [ ] works\n",
            "state": {"name": "Todo", "type": "unstarted"},
            "assignee": None,
            "labels": {"nodes": []},
            "children": {"nodes": []},
        }
        ok_f, reason_f = ad.is_ready_for_frontier(unlabeled)
        self.assertFalse(ok_f)
        self.assertEqual(reason_f, "missing_agent_ready_label")

        # Hierarchical Touches overlap: src/routes and src/routes/admin MUST NOT share a wave!
        iss_a = {"identifier": "TST-1", "description": "## Touches\n- src/routes\n"}
        iss_b = {"identifier": "TST-2", "description": "## Touches\n- src/auth\n"}
        iss_c = {"identifier": "TST-3", "description": "## Touches\n- src/routes/admin\n"}
        iss_d = {"identifier": "TST-4", "description": "## What to Build\nno touches declared"}
        wave, deferred = ad.select_non_overlapping_wave([iss_a, iss_b, iss_c, iss_d])
        self.assertEqual([i["identifier"] for i in wave], ["TST-1", "TST-2"])
        self.assertEqual([d["id"] for d in deferred], ["TST-3", "TST-4"])

    def test_no_renames_catches_git_mv_bypass_and_dirty_docs_allows_behind_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bare = Path(td) / "origin.git"
            repo = Path(td) / "demo"
            _git(Path(td), "init", "--bare", "-b", "main", str(bare))
            _git(Path(td), "clone", str(bare), str(repo))
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "tests" / "unit").mkdir(parents=True)
            (repo / "tests" / "unit" / "test_math.py").write_text("def test_add():\n    assert 1+1==2\n", encoding="utf-8")
            (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (repo / "GLOSSARY.md").write_text("# Glossary\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            _git(repo, "push", "-u", "origin", "main")
            base_sha = _git(repo, "rev-parse", "HEAD")

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
            self.assertEqual(ad.check_dirty_docs_in_main_repo(repo, "main"), [])

            # Test --no-renames catches git mv of test file & pytest.ini!
            _git(repo, "mv", "tests/unit/test_math.py", "math_checks.py")
            _git(repo, "mv", "pytest.ini", "old_pytest.ini")
            _git(repo, "commit", "-m", "sneaky rename")
            cand_sha = _git(repo, "rev-parse", "HEAD")

            prot = ad.check_protected_paths(repo, base_sha, cand_sha)
            self.assertTrue(any("pytest.ini" in x for x in prot))
            ti = ad.check_test_integrity_diff(repo, base_sha, cand_sha)
            self.assertTrue(any("tests/unit/test_math.py" in x for x in ti))

    def test_opencode_attaches_files_before_double_dash_and_captures_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            wt = d / "wt"
            run_dir = d / "run"
            wt.mkdir()
            run_dir.mkdir()
            extra_diff = run_dir / "diff-r1.patch"
            extra_diff.write_text("diff --git a/a.py b/a.py\n+x = 1\n", encoding="utf-8")
            captured_cmds: list[list[str]] = []

            def fake_run_cmd(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                captured_cmds.append(list(cmd))
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
                return subprocess.CompletedProcess(cmd, 0, stdout=ndjson, stderr="")

            traces: list[dict[str, Any]] = []
            with mock.patch.object(ad, "run_cmd", side_effect=fake_run_cmd):
                res = ad.invoke_harness(
                    ad.parse_harness_spec("opencode:sol@low"),
                    "Review diff",
                    worktree=wt,
                    run_dir=run_dir,
                    schema=ad.REVIEW_SCHEMA,
                    schema_file=run_dir / "s.json",
                    prompt_file=run_dir / "p.md",
                    out_file=run_dir / "o.json",
                    read_only=True,
                    expected_axis="standards",
                    session_label="review-standards-r1",
                    trace_sink=traces,
                    attach_files=[extra_diff, extra_diff],
                )
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

    def test_non_ascii_paths_utf8_replace_hooks_and_granular_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "docs" / "adr").mkdir(parents=True)
            (repo / "docs" / "adr" / "中文.md").write_text("---\nstatus: accepted\n---\n# 中文 ADR\n", encoding="utf-8")
            (repo / "packages" / "api").mkdir(parents=True)
            (repo / "packages" / "api" / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
            (repo / "mk").mkdir()
            (repo / "mk" / "test.mk").write_text("test:\n\ttrue\n", encoding="utf-8")
            (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (repo / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
            (repo / "app").mkdir()
            (repo / "app" / "tests.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n", encoding="utf-8")
            (repo / "go_test.go").write_text("package main\nfunc TestA(t *testing.T) {}\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            base_sha = _git(repo, "rev-parse", "HEAD")

            # Install a failing pre-commit hook -> orchestrator_commit MUST disable hooks and succeed!
            hook = repo / ".git" / "hooks" / "pre-commit"
            _write_exec(hook, "#!/bin/sh\necho hook_must_not_run >&2\nexit 1\n")

            # Snapshot MUST include Chinese ADR under default core.quotePath=true!
            std_dir = d / "std"
            files, hashes = ad.snapshot_baseline_standards(repo, base_sha, std_dir)
            self.assertIn("docs/adr/中文.md", files)
            self.assertIn("docs/adr/中文.md", hashes)

            # Mutate Chinese ADR, nested pyproject.toml, *.mk, pytest.ini, invalid package.json, delete app/tests.py, add t.SkipNow()
            (repo / "docs" / "adr" / "中文.md").write_text("# modified\n", encoding="utf-8")
            (repo / "packages" / "api" / "pyproject.toml").write_text("# tampered\n", encoding="utf-8")
            (repo / "mk" / "test.mk").write_text("test:\n\techo hacked\n", encoding="utf-8")
            (repo / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
            (repo / "package.json").write_text("{broken json", encoding="utf-8")
            (repo / "app" / "tests.py").unlink()
            (repo / "go_test.go").write_text("package main\nfunc TestA(t *testing.T) { t.SkipNow() }\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                cand_sha, sec_hits = ad.orchestrator_commit(repo, "TST-99", "normal title", 1)
            self.assertEqual(sec_hits, [])
            self.assertNotEqual(cand_sha, base_sha)

            # Authorizing ONLY `- gate: pytest.ini` MUST exempt pytest.ini while STILL blocking all other protected files & test skips!
            prot = ad.check_protected_paths(repo, base_sha, cand_sha, allowed_patterns=["pytest.ini"])
            self.assertFalse(any("pytest.ini" in x for x in prot), prot)
            self.assertTrue(any("docs/adr/中文.md" in x for x in prot), prot)
            self.assertTrue(any("packages/api/pyproject.toml" in x for x in prot), prot)
            self.assertTrue(any("mk/test.mk" in x for x in prot), prot)
            self.assertTrue(any("Invalid JSON in `package.json`" in x for x in prot), prot)

            ti = ad.check_test_integrity_diff(repo, base_sha, cand_sha, allowed_patterns=[])
            self.assertTrue(any("app/tests.py" in x for x in ti), ti)
            self.assertTrue(any("SkipNow" in x for x in ti), ti)

            # Non-ASCII secret file `目录/.env` MUST be blocked before staging!
            (repo / "目录").mkdir()
            (repo / "目录" / ".env").write_text("SECRET=1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                _, sec_hits2 = ad.orchestrator_commit(repo, "TST-99", "secret file test", 2)
            self.assertTrue(any("目录/.env" in x for x in sec_hits2), sec_hits2)

            # Non-UTF-8 subprocess output MUST NOT raise UnicodeDecodeError!
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                res_bin = ad.run_cmd([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xffabc')"], env_role="orchestrator")
            self.assertEqual(res_bin.returncode, 0)
            self.assertIn("abc", res_bin.stdout)

    def test_pr_feedback_trust_filter_pi_timeout_trace_and_review_json_retry(self) -> None:
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
                fb = ad.fetch_open_pr_feedback(d, "test/repo", "feat/x") or ""
            self.assertIn("fix null check", fb)
            self.assertIn("good inline", fb)
            self.assertNotIn("untrusted empty", fb)
            self.assertNotIn("untrusted none", fb)
            self.assertNotIn("bad inline", fb)

            # 2. Pi timeout MUST preserve session_id in sessions.jsonl!
            run_dir = d / "pi-run"
            run_dir.mkdir()
            traces: list[dict[str, Any]] = []
            with mock.patch.object(ad, "run_cmd", side_effect=subprocess.TimeoutExpired(["pi"], 5)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    ad.invoke_harness(
                        ad.parse_harness_spec("pi:sol@low"),
                        "check",
                        worktree=d,
                        run_dir=run_dir,
                        schema=ad.REVIEW_SCHEMA,
                        schema_file=run_dir / "s.json",
                        prompt_file=run_dir / "p.md",
                        out_file=run_dir / "o.json",
                        read_only=True,
                        expected_axis="standards",
                        session_label="pi-timeout-test",
                        trace_sink=traces,
                    )
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0]["status"], "timeout")
            self.assertTrue(traces[0].get("session_id"), "Pi timeout MUST keep generated session_id!")

            # 3. LinearClient.query retries TimeoutError before succeeding
            client = ad.LinearClient("lin_api_fake_1234567890", url="http://127.0.0.1:9/graphql")
            calls = {"n": 0}

            class _FakeResp:
                def __enter__(self) -> "_FakeResp":
                    return self
                def __exit__(self, *a: Any) -> None:
                    pass
                def read(self) -> bytes:
                    return json.dumps({"data": {"ok": True}}).encode("utf-8")

            def flaky_urlopen(req: Any, timeout: int = 30) -> _FakeResp:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("simulated read timeout")
                return _FakeResp()

            with mock.patch("urllib.request.urlopen", side_effect=flaky_urlopen), mock.patch("time.sleep"):
                res = client.query("query { ok }")
            self.assertEqual(res, {"ok": True})
            self.assertEqual(calls["n"], 2)

    def test_reused_branch_baseline_runs_in_detached_worktree_and_catches_red_base(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            bare = d / "origin.git"
            repo = d / "repo"
            _git(d, "init", "--bare", "-b", "main", str(bare))
            _git(d, "clone", str(bare), str(repo))
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            # Base commit has a FAILING test!
            (repo / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_fail(self): self.assertEqual(1, 2)\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "red base")
            _git(repo, "push", "-u", "origin", "main")

            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "cache")}):
                wt, branch, base_sha = ad.prepare_worktree(repo, "TST-BASE", "main", issue_uuid="uuid-base")
                # Feature branch fixes the test so head != base_sha (reused branch!)
                (wt / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1, 1)\n", encoding="utf-8")
                _git(wt, "add", "-A")
                _git(wt, "commit", "-m", "fix on feature")
                run_dir = d / "run"
                run_dir.mkdir()
                base_res = ad.check_base_baseline(
                    repo, ad.repo_cache_key(repo), wt, base_sha, "python3 -m unittest test_x.py", run_dir
                )
            self.assertTrue(base_res["checked"], "Reused feature branch MUST NOT silently skip baseline check!")
            self.assertFalse(base_res["passed"], "Red base_sha MUST fail baseline check in detached worktree!")

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
                    "branch": "feat/tst-p",
                    "base_branch": "main",
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

            # 2. agy worker mode MUST pass --disable-slash-commands and NOT --mode plan!
            agy_cmds: list[list[str]] = []
            def fake_agy_worker(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
                agy_cmds.append(list(cmd))
                out = {
                    "conversation_id": "11111111-1111-1111-1111-111111111111",
                    "status": "SUCCESS",
                    "structured_output": {"status": "completed", "summary": "ok", "tests_run": [], "discovered_issues": [], "blockers": ""},
                }
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
            with mock.patch.object(ad, "run_cmd", side_effect=fake_agy_worker):
                ad.invoke_harness(
                    ad.parse_harness_spec("agy:carbon"),
                    "work",
                    worktree=repo,
                    run_dir=run_dir,
                    schema=ad.WORKER_SCHEMA,
                    schema_file=run_dir / "ws.json",
                    prompt_file=run_dir / "wp.md",
                    out_file=run_dir / "wo.json",
                    read_only=False,
                    session_label="agy-worker-test",
                )
            self.assertIn("--disable-slash-commands", agy_cmds[0])
            self.assertNotIn("plan", agy_cmds[0])

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
                    # Simulate REAL signal handler setting lock-free _SHUTDOWN_SIGNAL before raising!
                    ad._SHUTDOWN_SIGNAL = signal.SIGINT
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
                                ad._SHUTDOWN_SIGNAL = 0
                rkey = ad.repo_cache_key(repo)
                ckpt = json.loads((cache_dir / "runs" / rkey / "TST-SIG" / "latest.json").read_text(encoding="utf-8"))
                self.assertEqual(ckpt["stage"], "pushing", "Publish-phase signal MUST NOT overwrite stage to 'error'!")
                self.assertTrue(ckpt["push_attempted"])
                # Even with REAL _SHUTDOWN_SIGNAL set, finally MUST write dispatch:publish-incomplete + comment!
                self.assertEqual(iss["state"]["name"], "In Progress")
                self.assertIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
                self.assertNotIn("dispatch:failed", [l["name"] for l in iss["_labels"]])
                self.assertTrue(any("Publish Interrupted" in c["body"] for c in ls_state.comments[iss["id"]]))
            finally:
                srv.shutdown()
                srv.server_close()

    def test_v5_regressions_opus_removed_opencode_verdict_mixed_tests_globs_remotes_and_parallel_baseline(self) -> None:
        # 1. Opus/Claude aliases completely removed from agy harness
        self.assertNotIn("opus", ad.HARNESS_ALIASES["agy"])
        self.assertNotIn("sonnet", ad.HARNESS_ALIASES["agy"])
        self.assertEqual(ad.parse_harness_spec("agy:neon").vendor, "google")

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
        parsed_txt = ad.parse_opencode_ndjson(oc_stream)
        verdict_obj = ad.validate_schema(ad.extract_json_object(parsed_txt), ad.REVIEW_SCHEMA, expected_axis="standards")
        self.assertEqual(verdict_obj["verdict"], "fail")
        # Also verify extract_json_object alone when BOTH fenced pass and trailing bare fail are in the same message!
        same_msg = (
            '```json\n{"axis":"spec","verdict":"pass","hard_findings":[],"suggestions":[],"test_integrity_violation":false,"summary":"early"}\n```\n'
            'Actually failed:\n{"axis":"spec","verdict":"fail","hard_findings":["Missing AC"],"suggestions":[],"test_integrity_violation":false,"summary":"final"}'
        )
        self.assertEqual(ad.extract_json_object(same_msg)["verdict"], "fail")

        # 3. Path-segment globs & fenced code blocks in Authorized Gate/Test Changes
        fenced_brief = (
            "## Authorized Gate/Test Changes\n"
            "Example:\n"
            "```markdown\n"
            "- gate: Makefile\n"
            "```\n"
            "> - gate: pyproject.toml\n"
            "- gate: ?*\n"
            "- gate: */*\n"
            "- test: tests/*.py\n"
        )
        g_pats, t_pats = ad.parse_authorized_changes(fenced_brief)
        self.assertEqual(g_pats, [], "Fenced/quoted/wildcard gate patterns MUST be rejected!")
        self.assertEqual(t_pats, ["tests/*.py"])
        self.assertTrue(ad.path_matches_any_pattern("tests/test_a.py", t_pats))
        self.assertFalse(ad.path_matches_any_pattern("tests/nested/test_a.py", t_pats), "Single * MUST NOT cross /!")

        # 4. Frontier MUST reject custom non-Todo unstarted states like 'Awaiting approval'
        custom_unstarted = {
            "identifier": "TST-WAIT",
            "title": "Needs approval",
            "state": {"name": "Awaiting approval", "type": "unstarted"},
            "assignee": None,
            "labels": {"nodes": [{"name": "agent-ready"}]},
            "children": {"nodes": []},
        }
        self.assertFalse(ad.is_ready_for_frontier(custom_unstarted)[0])

        # 5. GitHub URL parser handles SSH-over-443 & trailing .git/, and unknown remote fails closed
        self.assertEqual(
            ad.parse_github_owner_repo_url("ssh://git@ssh.github.com:443/K1ndredzzz/agent-workflow.git/"),
            "K1ndredzzz/agent-workflow",
        )
        self.assertEqual(
            ad.parse_github_owner_repo_url("https://github.com/K1ndredzzz/agent-workflow.git/"),
            "K1ndredzzz/agent-workflow",
        )
        self.assertIsNone(ad.parse_github_owner_repo_url("git@github-work:org/repo.git"))

        # 6. Mixed Python unittest+pytest repo MUST NOT silently fall back to unittest discover; JS lint-only with tests/ returns None!
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            py_repo = d / "py_mixed"
            (py_repo / "tests").mkdir(parents=True)
            (py_repo / "tests" / "test_mix.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1,1)\n"
                "def test_pytest_fail():\n    assert False\n",
                encoding="utf-8",
            )
            with mock.patch.object(ad.shutil, "which", return_value=None):
                self.assertIsNone(ad.detect_verify_command(py_repo, None))

            js_repo = d / "js_lint_only"
            (js_repo / "tests").mkdir(parents=True)
            (js_repo / "package.json").write_text('{"scripts": {"lint": "eslint ."}}', encoding="utf-8")
            self.assertIsNone(ad.detect_verify_command(js_repo, None))

            # 7. Standalone review blocks added secret BEFORE calling reviewer harness (0 LLM calls!)
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

            # 8. Reviewer transient RuntimeError auto-retries once!
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

            # 9. Parallel 2-thread check_base_baseline on reused branch (cache miss, same base) MUST NOT delete each other's temp worktree!
            bare = d / "par.git"
            prepo = d / "par"
            _git(d, "init", "--bare", "-b", "main", str(bare))
            _git(d, "clone", str(bare), str(prepo))
            _git(prepo, "config", "user.email", "t@example.com")
            _git(prepo, "config", "user.name", "T")
            (prepo / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1,1)\n", encoding="utf-8")
            _git(prepo, "add", "-A")
            _git(prepo, "commit", "-m", "init")
            _git(prepo, "push", "-u", "origin", "main")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(d / "par_cache")}):
                wt1, _, base_sha = ad.prepare_worktree(prepo, "PAR-1", "main", issue_uuid="u1")
                wt2, _, _ = ad.prepare_worktree(prepo, "PAR-2", "main", issue_uuid="u2")
                for wt in (wt1, wt2):
                    (wt / "f.txt").write_text("x\n", encoding="utf-8")
                    _git(wt, "add", "f.txt")
                    _git(wt, "commit", "-m", "ahead")
                rkey = ad.repo_cache_key(prepo)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(ad.check_base_baseline, prepo, rkey, wt1, base_sha, "python3 -m unittest test_ok.py", d / "r1")
                    f2 = pool.submit(ad.check_base_baseline, prepo, rkey, wt2, base_sha, "python3 -m unittest test_ok.py", d / "r2")
                    res1, res2 = f1.result(), f2.result()
                self.assertTrue(res1["passed"] and res2["passed"])
                # One ran baseline, the other hit the exact baseline cache cleanly under per-key lock!
                self.assertEqual(sorted([bool(res1.get("cached")), bool(res2.get("cached"))]), [False, True])

            # 10. Multi-PR on same head fails closed BEFORE git push!
            two_prs = [
                {"number": 1, "url": "u1", "state": "OPEN", "isDraft": True, "baseRefName": "main", "headRefOid": "a" * 40},
                {"number": 2, "url": "u2", "state": "OPEN", "isDraft": False, "baseRefName": "release", "headRefOid": "a" * 40},
            ]
            with mock.patch.object(ad, "fetch_open_prs_for_branch", return_value=two_prs):
                with self.assertRaisesRegex(RuntimeError, "Multiple open PRs share head branch"):
                    ad.fetch_open_pr_for_branch(prepo, "org/repo", "feat/x")

    def test_auth_redaction_preserves_english_prose_while_redacting_real_credentials(self) -> None:
        benign_phrases = (
            "Add basic validation to login form",
            "Implement basic authentication flow",
            "Add basic end-to-end tests",
            "Basic configuration helper",
            "Support bearer authentication in API",
            "Support Bearer Authorization header",
            "Support basic Base64URL encoding",
        )
        for phrase in benign_phrases:
            self.assertEqual(ad.redact_secrets(phrase), phrase, f"False positive on: {phrase}")
            self.assertEqual(ad.scan_text_for_secrets(phrase, "title"), [], f"False block on: {phrase}")

        secret_cases = (
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Authorization: Basic simpleword", "simpleword"),
            ("Authorization: Bearer simpleword", "simpleword"),
            ('{"authorization": "Bearer simpleword"}', "simpleword"),
            ("Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Basic YWRtaW46c2VjcmV0", "YWRtaW46c2VjcmV0"),
            ("Bearer invented-only-example", "invented-only-example"),
            ("Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
        )
        for text, secret in secret_cases:
            redacted = ad.redact_secrets(text)
            self.assertIn("<REDACTED_SECRET>", redacted, f"Missed redaction for: {text}")
            self.assertNotIn(secret, redacted, f"Secret leaked in: {text}")
            self.assertTrue(ad.scan_text_for_secrets(text, "header"), f"Missed scan hit for: {text}")

    def test_redacted_log_json_truncated_pem_does_not_drop_subsequent_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "stream.log"
            payload = (
                json.dumps({"type": "tool_result", "output": "-----BEGIN PRIVATE KEY-----\nMIIE_secret_body"}) + "\n"
                + json.dumps({"type": "thread.started", "thread_id": "11111111-2222-3333-4444-555555555555"}) + "\n"
                + "plain line after json\n"
            )
            with contextlib.ExitStack() as stack:
                logger = ad._RedactedLog(log_path, stack)
                logger.write(payload.encode("utf-8"))
                logger.write(b"")
            written = log_path.read_text(encoding="utf-8")
            self.assertNotIn("MIIE_secret_body", written)
            self.assertIn("<REDACTED_SECRET>", written)
            self.assertIn("11111111-2222-3333-4444-555555555555", written)
            self.assertIn("plain line after json", written)

    def test_harness_events_and_pi_archive_tolerate_malformed_lines_without_masking_errors(self) -> None:
        # Stray non-JSON line starting with '{' must not break NDJSON parsing
        verdict = json.dumps({"status": "completed"})
        codex_out = "\n".join([
            "{non-json diagnostic header",
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": verdict}}),
            json.dumps({"type": "turn.completed"}),
        ])
        self.assertEqual(ad.parse_codex_ndjson(codex_out), verdict)

        # Malformed newline-terminated line in Pi session memfd must not mask outer exception or drop valid lines
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            archive = d / "pi.jsonl"
            valid_entry = {"type": "message", "id": "m1", "message": {"content": "ok"}}
            with self.assertRaisesRegex(RuntimeError, "outer harness failure"):
                with ad._pi_session_archive(archive, "22222222-3333-4444-5555-666666666666", d) as native:
                    subprocess.run(
                        [sys.executable, "-B", "-c", "import sys; open(sys.argv[1], 'a').write(sys.stdin.read())", native],
                        input="\n{malformed json line\n" + json.dumps(valid_entry) + "\n",
                        text=True,
                        check=True,
                    )
                    raise RuntimeError("outer harness failure")
            lines = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[1]["id"], "m1")

    def test_run_cmd_bounded_buffer_caps_memory_while_streaming_full_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            out_log = d / "out.log"
            # Generate 16KB of numbered lines with a patched 2KB DISPLAY_OUTPUT_CAP
            code = "for i in range(400):\n    print(f'line-{i:04d}-' + 'x' * 32)\n"
            with mock.patch.object(ad, "DISPLAY_OUTPUT_CAP", 2048):
                res = ad.run_cmd(
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

    def test_touches_normalization_fences_headings_wildcards_and_symlink_and_pkg_guards(self) -> None:
        brief = (
            "```md\n## Touches\n- fake/from-codeblock\n```\n"
            "## Touches\n"
            "- `./src//api/`, `pkg\\core/*`\n"
            "# Appendix\n"
            "- unrelated/outside-section\n"
        )
        self.assertEqual(ad.parse_touches(brief), {"src/api", "pkg/core"})
        self.assertTrue(ad.touches_overlap(ad.parse_touches("## Touches\n- ./src/api\n"), {"src/api/v1"}))
        # Wildcard root fails closed as unscoped empty set
        self.assertEqual(ad.parse_touches("## Touches\n- *\n"), set())
        self.assertEqual(ad.parse_touches("## Touches\n- **/*\n"), set())

        # package.json containing non-object valid JSON triggers violation cleanly instead of AttributeError;
        # symlink pointing to a secret filename is blocked before git write
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "t@example.invalid")
            _git(repo, "config", "user.name", "Test")
            (repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            _git(repo, "add", "package.json")
            _git(repo, "commit", "-m", "init")
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "package.json").write_text("[]\n", encoding="utf-8")
            _git(repo, "add", "package.json")
            _git(repo, "commit", "-m", "array pkg")
            head = _git(repo, "rev-parse", "HEAD")
            viols = ad.check_protected_paths(repo, base, head)
            self.assertTrue(any("Invalid JSON object" in v for v in viols))

            (repo / "link_secret").symlink_to("/etc/app/.env")
            hits = ad._scan_before_git_write(repo)
            self.assertTrue(any("secret symlink target" in h for h in hits))


# ---------------------------------------------------------------------------
# End-to-End Subprocess CLI Fault-Injection Suite
# ---------------------------------------------------------------------------

class TestEndToEndCLIFaultInjection(IsolatedDispatchCase):
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
elif mode == "parallel_safe":
    import time as _t
    if cwd.name.endswith("P2"):
        _t.sleep(0.5)
    (cwd / f"mod_{{cwd.name.lower()}}.py").write_text(f"VAL = '{{cwd.name}}'\\n", encoding="utf-8")
elif mode == "crash":
    print("simulated codex crash", file=sys.stderr)
    sys.exit(1)
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
    sha = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
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

    def test_all_shutdown_signals_during_child_registration_reap_the_process(self) -> None:
        # Deliver the signal after Popen returns and before the child is visible in the registry.
        probe = """
import os, runpy, signal, sys
m = runpy.run_path(sys.argv[1])
m['_install_signal_handlers']()
register = m['_register_active_pgid']
pids = []
def interrupt_registration(pid):
    pids.append(pid)
    os.kill(os.getpid(), int(sys.argv[2]))
    register(pid)
m['run_cmd'].__globals__['_register_active_pgid'] = interrupt_registration
try:
    m['run_cmd']([sys.executable, '-B', '-c', 'import time; time.sleep(4)'])
except (SystemExit, KeyboardInterrupt):
    assert m['_is_shutdown_requested']() and pids
    try:
        os.kill(pids[0], 0)
    except ProcessLookupError:
        sys.exit(0)
sys.exit(1)
"""
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = subprocess.run([sys.executable, "-B", "-c", probe, str(BIN_PATH), str(sig.value)],
                                       cwd=self.root, capture_output=True, text=True, timeout=6)
                self.assertEqual(child.returncode, 0, child.stderr)

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
        self.assertTrue(sig_marker.exists(), "git push wrapper did not fire SIGTERM!")
        self.assertEqual(res1.returncode, 128 + signal.SIGTERM, res1.stderr + res1.stdout)
        # Checkpoint MUST keep resumable stage (pushing/pushed), NOT 'error'!
        rkey = ad.repo_cache_key(self.repo)
        ckpt = json.loads((self.cache_dir / "runs" / rkey / "TST-TERM" / "latest.json").read_text(encoding="utf-8"))
        self.assertIn(ckpt["stage"], ("pushing", "pushed"))
        # Linear MUST have dispatch:publish-incomplete + comment and MUST NOT have rolled back to Todo!
        self.assertEqual(iss["state"]["name"], "In Progress")
        self.assertIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
        self.assertNotIn("dispatch:failed", [l["name"] for l in iss["_labels"]])
        self.assertTrue(any("Publish Interrupted" in c["body"] for c in self.linear_state.comments[iss["id"]]))

        # Now `resume` converges cleanly to passed!
        res2 = self._run_cli(["resume", "TST-TERM", "--repo", str(self.repo)])
        self.assertEqual(res2.returncode, 0, res2.stderr + res2.stdout)
        out2 = json.loads(res2.stdout)
        self.assertEqual(out2["status"], "passed")
        self.assertEqual(iss["state"]["name"], "In Review")
        self.assertNotIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])

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
            cx_skills = d / "codex_skills"
            cx_agents = d / "codex" / "AGENTS.md"
            bin_dst = d / "bin" / "agent-dispatch"
            (ag_skills / "my-custom-skill").mkdir(parents=True)
            (ag_skills / "my-custom-skill" / "SKILL.md").write_text("keep me", encoding="utf-8")
            (ag_skills / "my-grill").symlink_to(src_copy / "skills" / "grill")
            cx_agents.parent.mkdir(parents=True)
            cx_agents.write_text("custom real AGENTS.md", encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "AGENT_SKILLS_DIR": str(ag_skills),
                    "CODEX_SKILLS_DIR": str(cx_skills),
                    "CODEX_AGENTS_MD": str(cx_agents),
                    "BIN_TARGET": str(bin_dst),
                }
            )
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            initial_links = {p: p.lstat().st_ino for p in ag_skills.iterdir()}
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            self.assertEqual(initial_links, {p: p.lstat().st_ino for p in ag_skills.iterdir()})
            self.assertEqual((ag_skills / "my-grill").resolve(), src_copy / "skills" / "grill")
            # Unrelated skill preserved!
            self.assertTrue((ag_skills / "my-custom-skill" / "SKILL.md").is_file())
            # Real AGENTS.md backed up, not destroyed!
            backups = list(cx_agents.parent.glob("AGENTS.md.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "custom real AGENTS.md")
            # Orchestrator skills NOT symlinked into codex skills!
            self.assertFalse((cx_skills / "dispatch").exists())
            self.assertFalse((cx_skills / "slice").exists())
            self.assertTrue((cx_skills / "grill").is_symlink())
            self.assertTrue((ag_skills / "dispatch").is_symlink())

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

        # 3. Failed baseline on fresh worktree (head == base_sha) leaves worktree clean!
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(self.cache_dir)}):
            wt_fresh, _, _ = ad.prepare_worktree(self.repo, "TST-RED", "main", issue_uuid="uuid-red")
            res_red = ad.check_base_baseline(
                self.repo,
                rkey,
                wt_fresh,
                base,
                "echo dirty > app.py && echo junk > temp.out && exit 1",
                self.root / "run-red",
            )
            self.assertFalse(res_red["passed"])
            self.assertEqual(_git(wt_fresh, "status", "--porcelain"), "")

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
