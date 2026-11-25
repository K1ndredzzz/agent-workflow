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
        self.outage_flag_file: Path | None = None
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
        if self.outage_flag_file and self.outage_flag_file.exists():
            return {"errors": [{"message": "Simulated Linear API/network outage"}]}
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
    def test_api_key_headers_are_redacted_across_diagnostic_formats(self) -> None:
        token = "opaque" + "-synthetic!value"
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            for header in ("X-API-Key", "x-goog-api-key", "X-Api-kEy"):
                event = {"request": {"headers": {header: token}}, "status": "ok"}
                plain = f"Request header: {header}: {token}"
                self.assertTrue(ad.scan_text_for_secrets(plain, "test"))
                self.assertNotIn(token, ad.redact_secrets(json.dumps(event)))
                self.assertEqual(json.loads(ad.safe_json(event))["request"]["headers"][header], "<REDACTED_SECRET>")
                with contextlib.ExitStack() as stack:
                    writer = ad._RedactedLog(folder / "log", stack)
                    writer.write((plain + "\n" + json.dumps(event) + "\n").encode())
                self.assertNotIn(token, (folder / "log").read_text())
            self.assertEqual(ad.redact_secrets("Document X-API-Key header support."), "Document X-API-Key header support.")
            for benign_code in (
                'headers = {"x-api-key": api_key}',
                'headers = {"x-api-key": my_api_key}',
                'headers = {"x-api-key": self.api_key}',
                'headers = {"x-api-key": os.environ["API_KEY"]}',
                'headers = {"x-api-key": process.env.API_KEY}',
                'headers = {"x-api-key": ""}',
                'interface Headers { "x-api-key": string; }',
                'x-api-key: <your-api-key>',
                'X-API-Key: ${API_KEY}',
            ):
                self.assertEqual(ad.redact_secrets(benign_code), benign_code, f"False positive on code: {benign_code}")
                self.assertEqual(ad.scan_text_for_secrets(benign_code, "code"), [], f"False block on code: {benign_code}")

    def test_raw_http_credentials_are_redacted_before_logging_and_git_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "base")
            for header, value in [("Authorization", "token synthetic.opaque.value"),
                                  ("Proxy-Authorization", "Basic syntheticopaque"),
                                  ("X-API-Key", "synthetic.opaque.value"), ("X-Goog-Api-Key", "synthetic_opaque_value")]:
                with self.subTest(header=header):
                    line = f"{header}: {value}\r\n"
                    (repo / "request.txt").write_text(line)
                    self.assertTrue(ad._scan_before_git_write(repo))
                    with contextlib.ExitStack() as stack:
                        log = Path(td) / "request.log"
                        writer = ad._RedactedLog(log, stack)
                        writer.write(line.encode())
                    self.assertNotIn(value.split()[-1], log.read_text())
            for source in (
                "Authorization: Bearer ${ACCESS_TOKEN}",
                "  Authorization: process.env.ACCESS_TOKEN,",
                "  Authorization: `Bearer ${token}`,",
                "- X-API-Key: header used for client authentication",
                "- Authorization: Bearer token validation rules",
                "Authorization: Basic authentication is required for this endpoint.",
                "  x-api-key: ${{ secrets.API_KEY }}",
                "  x-api-key: {{ api_key }}",
                "  x-api-key: string # API key header",
                '  x-api-key: ""',
            ):
                self.assertEqual(ad.redact_secrets(source), source, f"False positive on: {source}")
                self.assertFalse(ad.scan_text_for_secrets(source, "template"), f"False block on: {source}")

    def test_header_examples_pass_git_gates_but_literal_values_do_not(self) -> None:
        api_key_header = "X-API-Key"
        examples = {
            "client.py": 'headers = {"X-API-Key": API_KEY_VALUE}',
            "client.js": 'const headers = { "X-API-Key": clientKey };',
            "gateway.yaml": "key: &gateway_key ${GATEWAY_KEY}\nheaders:\n"
                            f"  {api_key_header}: *gateway_key # secret-scan:allow",
            "template.yaml": 'headers:\n  X-API-Key: "{{ .Values.gateway.apiKey }}"',
            "README.md": "- X-API-Key: Identifies the caller for authentication.",
        }
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-m", "base")
            for filename, example in examples.items():
                with self.subTest(filename=filename):
                    (repo / filename).write_text(example + "\n")
                    self.assertFalse(ad._scan_before_git_write(repo))
            self.assertFalse(ad.scan_text_for_secrets("Document X-API-Key header support", "title"))
            _git(repo, "add", ".")
            self.assertFalse(ad.scan_staged_for_secrets(repo))
            for literal in ("clientKey", "Identifies", "*gateway_key"):
                (repo / "request.http").write_text(f"X-API-Key: {literal}\r\n")
                self.assertTrue(ad._scan_before_git_write(repo))
                self.assertNotIn(literal, ad.redact_secrets(f"X-API-Key: {literal}"))
            for source in ('headers: {"X-API-Key": clientKey}',
                           'headers = {"X-API-Key": "clientKey"}',
                           "key: &gateway_key clientKey\nheaders:\n  X-API-Key: *gateway_key",
                           "- X-API-Key: Identifies"):
                self.assertTrue(ad.scan_text_for_secrets(source, "literal"))

    def test_json_quoted_authorization_credentials_detected_and_redacted_across_scans_and_review(self) -> None:
        token = "synthetic_pd_tok_987654321"
        fixture_entries = [
            ("Authorization", f"Token token={token}"),
            ("Authorization", f'Token token="{token}"'),
            ("Authorization", f"Token {token}"),
            ("Authorization", f"ApiKey user:{token}"),
            ("Authorization", f'OAuth oauth_consumer_key="ck_{token}", oauth_token="{token}"'),
            ("Authorization", f"Bearer {token}"),
            ("Authorization", "Basic dXNlcjpzeW50aGV0aWM="),
            ("Proxy-Authorization", f"Token token={token}"),
            ("Proxy-Authorization", f"ApiKey {token}"),
            ("Proxy-Authorization", f"OAuth {token}"),
            ("Proxy-Authorization", f"Bearer {token}"),
            ("Proxy-Authorization", "Basic cHJveHk6c3ludGhldGlj"),
        ]
        # Same synthetic JSON fixture across text scan, staged scan, history scan, and reviewer diff redaction.
        settings_json = json.dumps(
            {
                "pagerduty": {"headers": {"Authorization": f"Token token={token}"}},
                "proxy": {"headers": {"Proxy-Authorization": f"ApiKey {token}"}},
            },
            indent=2,
        ) + "\n"
        self.assertTrue(ad.scan_text_for_secrets(settings_json, "settings.json"))
        redacted_json = ad.redact_secrets(settings_json)
        self.assertNotIn(token, redacted_json)
        self.assertIn("<REDACTED_SECRET>", redacted_json)
        self.assertEqual(ad.redact_secrets(redacted_json), redacted_json)

        for header, value in fixture_entries:
            for sample in (
                json.dumps({header: value}),
                f"{{'{header}': '{value}'}}",
                f"{header}: {json.dumps(value)}",
            ):
                with self.subTest(header=header, sample=sample):
                    self.assertTrue(ad.scan_text_for_secrets(sample, "config"), f"Missed secret in {sample!r}")
                    redacted = ad.redact_secrets(sample)
                    self.assertNotIn(token, redacted)
                    self.assertNotIn("dXNlcjpzeW50aGV0aWM=", redacted)
                    self.assertNotIn("cHJveHk6c3ludGhldGlj", redacted)
                    self.assertEqual(ad.redact_secrets(redacted), redacted)

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "commit", "--allow-empty", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")

            cfg = repo / "settings.json"
            cfg.write_text(settings_json, encoding="utf-8")
            self.assertTrue(ad._scan_before_git_write(repo))
            _git(repo, "add", "settings.json")
            self.assertTrue(ad.scan_staged_for_secrets(repo))
            _git(repo, "commit", "-m", "add synthetic json credentials")
            introduced = _git(repo, "rev-parse", "HEAD")
            self.assertTrue(ad.scan_range_for_secrets(repo, base, introduced))

            # Removing the credentials clears incremental history scan, while reviewer patch redacts removed secrets.
            cfg.write_text('{"pagerduty": {"headers": {"Authorization": "Token token=${PAGERDUTY_TOKEN}"}}}\n', encoding="utf-8")
            _git(repo, "commit", "-am", "replace with placeholder")
            cleaned = _git(repo, "rev-parse", "HEAD")
            self.assertFalse(ad.scan_range_for_secrets(repo, introduced, cleaned))
            self.assertTrue(ad.scan_range_for_secrets(repo, base, cleaned))

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

        # Zero false positives on placeholders, benign type words, concatenation prefixes, and English prose.
        for benign in (
            '{"Authorization": "Token token=${PAGERDUTY_TOKEN}"}',
            '{"Authorization": "Token token=\\"${PAGERDUTY_TOKEN}\\""}',
            '{"Authorization": "Token token=<your-token>"}',
            '{"Authorization": "Token token={{ api_token }}"}',
            '{"Authorization": "Token token=$PAGERDUTY_TOKEN"}',
            '{"Authorization": "Bearer ${ACCESS_TOKEN}"}',
            '{"Authorization": "ApiKey ${{ secrets.API_KEY }}"}',
            '{"Authorization": "OAuth {{ .Values.oauthToken }}"}',
            '{"Proxy-Authorization": "Basic <base64-creds>"}',
            '{"Authorization": "Token string"}',
            '{"Authorization": "ApiKey optional"}',
            '{"Authorization": "Bearer required"}',
            '{"Authorization": "Token token=string"}',
            '{"Authorization": "OAuth oauth_token=\\"${OAUTH_TOKEN}\\", oauth_version=\\"1.0\\", realm=\\"api\\""}',
            'headers = {"Authorization": "Bearer " + token}',
            'headers = {"Authorization": "Token token=" + api_token}',
            '{"Authorization": "Bearer token validation rules"}',
            '{"Authorization": "Basic authentication is required for this endpoint."}',
            '{"Authorization": "OAuth 2.0 access token"}',
            'Authorization: Token token=${PAGERDUTY_TOKEN}',
        ):
            with self.subTest(benign=benign):
                self.assertEqual(ad.redact_secrets(benign), benign, f"False positive on {benign!r}")
                self.assertEqual(ad.scan_text_for_secrets(benign, "benign"), [], f"False block on {benign!r}")

    def test_yaml_alias_credentials_fail_closed_in_text(self) -> None:
        for name, value, flow_mapping in (
            ("X-API-Key", "*gateway_key", False),
            ("Authorization", "Token *gateway_key", False),
            ("Authorization", "*gateway_key", False),
            ("X-API-Key", "*k", True),
            ("Authorization", "*k", True),
        ):
            header = f'  headers: {{"{name}": {value}}}' if flow_mapping else f"  {name}: {value}"
            with self.subTest(header=header):
                source = "key: &gateway_key ${GATEWAY_KEY}\nshort: &k <PLACEHOLDER>\n" + header + "\n"
                self.assertEqual(ad.scan_text_for_secrets(source, "yaml"), ["Potential secret token in yaml"])

    def test_secret_scan_allow_exempts_only_heuristics_on_the_marked_line(self) -> None:
        marker = " # secret-scan:allow"
        finding = ["Potential secret token in source"]
        for prefix, value, suffix in (
            ("X-API-Key: ", "synthetic.opaque.value", ""),
            ("Authorization: ", "*key", ""),
            ("X-API-Key: ", "*key", ""),
            ('{"Authorization": "Token ', "synthetic.opaque.value", '"}'),
            ('{"X-API-Key": "', "synthetic.opaque.value", '"}'),
            ("Bearer ", "synthetic.opaque.value", ""),
            ("Basic ", "dXNlcjpmaXh0dXJl", ""),
        ):
            with self.subTest(prefix=prefix, value=value):
                line = f"{prefix}{value}{suffix}"
                marked = line + marker
                self.assertEqual(ad.scan_text_for_secrets(line, "source"), finding)
                self.assertEqual(ad.scan_text_for_secrets(marked, "source"), [])
                self.assertEqual(ad.scan_text_for_secrets(f"{marked}\n{line}", "source"), finding)
                redacted = ad.redact_secrets(marked)
                self.assertIn("<REDACTED_SECRET>", redacted)
                self.assertNotIn(value, redacted)
                self.assertEqual(ad.redact_data({"message": [marked]}), {"message": [redacted]})
                self.assertEqual(json.loads(ad.safe_json({"message": marked})), {"message": redacted})

    def test_secret_scan_allow_in_worktree_index_and_history(self) -> None:
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                _git(repo, "init", "-b", "main")
                _git(repo, "config", "user.name", "Test")
                _git(repo, "config", "user.email", "test@example.invalid")
                tracked = repo / "gateway.yaml"
                untracked = repo / "example.yaml"
                declaration = "key: &k ${GATEWAY_KEY}\n"
                tracked.write_bytes(declaration.encode(encoding))
                _git(repo, "add", tracked.name)
                _git(repo, "commit", "-m", "existing placeholder")
                for prefix, value in (
                    ("X-API-Key: ", "*k"),
                    ("Authorization: ", "*k"),
                    ("X-API-Key: ", "synthetic.opaque.value"),
                ):
                    for marker in ("", " # secret-scan:allow"):
                        with self.subTest(header=prefix, value=value, marked=bool(marker)):
                            content = f"{declaration}headers:\n  {prefix}{value}{marker}\n".encode(encoding)
                            untracked.write_bytes(content)
                            self.assertEqual(ad._scan_before_git_write(repo), [] if marker else [
                                "Potential secret token in untracked file content",
                            ])
                            untracked.unlink()
                            tracked.write_bytes(content)
                            expected = [] if marker else ["Potential secret token in added file content"]
                            self.assertEqual(ad._scan_before_git_write(repo), expected)
                            _git(repo, "add", tracked.name)
                            self.assertEqual(ad.scan_staged_for_secrets(repo), expected)
                            _git(repo, "commit", "-m", "change synthetic config")
                            self.assertEqual(ad.scan_range_for_secrets(repo, "HEAD^", "HEAD"), expected)

    def test_secret_scan_allow_preserves_non_bypassable_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "commit", "--allow-empty", "-m", "base")

            marker = " # secret-scan:allow"
            token = "lin_api_" + "syntheticvalue123456"
            live_value = "synthetic process credential"
            key_kind = "PRIVATE KEY"
            pem = f"-----BEGIN {key_kind}-----{marker}\nsynthetic-body\n-----END {key_kind}-----"
            with mock.patch.dict(os.environ, {"UNIT_API_KEY": live_value}):
                for filename, content, credential in (
                    ("token.txt", f"note{marker} {token}", token),
                    ("key.txt", pem, "synthetic-body"),
                    ("live.txt", f"note{marker} {live_value}", live_value),
                    (".env", f"SETTING=ordinary{marker}", None),
                ):
                    with self.subTest(filename=filename):
                        expected_file = ["Potential secret file: .env"]
                        expected_content = ["Potential secret token in added file content"]
                        if credential is not None:
                            self.assertEqual(ad.scan_text_for_secrets(content, "source"), [
                                "Potential secret token in source",
                            ])
                            self.assertNotIn(credential, ad.redact_secrets(content))
                        (repo / filename).write_text(content + "\n", encoding="utf-8")
                        self.assertEqual(ad._scan_before_git_write(repo), expected_file if credential is None else [
                            "Potential secret token in untracked file content",
                        ])
                        _git(repo, "add", filename)
                        expected = expected_file if credential is None else expected_content
                        self.assertEqual(ad.scan_staged_for_secrets(repo), expected)
                        _git(repo, "commit", "-m", "introduce synthetic fixture")
                        self.assertEqual(ad.scan_range_for_secrets(repo, "HEAD^", "HEAD"), expected)

            _git(repo, "commit", "--allow-empty", "-m", "Bearer " + "synthetic.opaque.value" + marker)
            self.assertEqual(ad.scan_range_for_secrets(repo, "HEAD^", "HEAD"), [
                "Potential secret token in commit message",
            ])

    def test_nested_authorization_headers_are_redacted_in_structured_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            token = "syntheticopaque"
            event = {"request": {"AUTHORIZATION": "Bearer " + token,
                                 "Proxy-Authorization": "Basic " + token},
                     "description": "Support bearer authentication"}
            log = folder / "log"
            with contextlib.ExitStack() as stack:
                writer = ad._RedactedLog(log, stack)
                writer.write((json.dumps(event) + "\n").encode())
            ad.append_session_trace(folder, event, None)
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
                    ad.parse_codex_ndjson("\n".join(map(json.dumps, [*items, {"type": "turn.completed"}])))

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

    def test_final_array_and_corrupt_protocol_event_cannot_approve_a_review(self) -> None:
        verdict = {"axis": "standards", "verdict": "pass", "hard_findings": [], "suggestions": [],
                   "test_integrity_violation": False, "summary": "ok"}
        for output in (json.dumps([verdict]), json.dumps(verdict) + "\nFinal: []"):
            stream = [{"type": "text", "part": {"text": output}}, {"type": "step_finish", "part": {"reason": "stop"}}]
            final_text = ad.parse_opencode_ndjson("\n".join(map(json.dumps, stream)))
            with self.assertRaises(ValueError):
                ad.validate_schema(ad.extract_json_object(final_text), ad.REVIEW_SCHEMA)
        interim = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(verdict)}}
        stream = "\n".join([json.dumps(interim), '{"type":"item.completed","item":', '{"type":"turn.completed"}'])
        with self.assertRaises(ValueError):
            ad.parse_codex_ndjson(stream)

    def test_truncated_final_object_cannot_reuse_draft_or_nested_verdict(self) -> None:
        draft = '{"verdict":"pass"}'
        for tail in ('{"verdict":"fail"', '{"details":{"verdict":"pass"}', '{', '{"summary":"unfinished'):
            with self.subTest(tail=tail):
                for response in (draft + "\nFinal answer:\n" + tail, "```json\n" + draft + "\n```\n" + tail):
                    with self.assertRaises(ValueError):
                        ad.extract_json_object(response)
        self.assertEqual(ad.extract_json_object('draft {"bad": } final {"verdict":"fail"} done'),
                         {"verdict": "fail"})

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

    def test_polyglot_verify_runs_python_after_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_backend.py").write_text(
                "import unittest\nclass TestBackend(unittest.TestCase):\n    def test_fail(self): self.fail('backend check')\n"
            )
            (root / "package.json").write_text('{"scripts":{"test":"node js-test.js"}}')
            _write_exec(root / "npm", "#!/bin/sh\ntouch js-ran\n")
            (root / "Cargo.toml").touch()
            (root / "go.mod").touch()
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}):
                command = ad.detect_verify_command(root, None)
                self.assertIn("go test ./...", command)
                self.assertIn("cargo test", command)
                (root / "Cargo.toml").unlink()
                (root / "go.mod").unlink()
                result = subprocess.run(ad.detect_verify_command(root, None), cwd=root, shell=True, capture_output=True)
            self.assertTrue((root / "js-ran").exists())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"backend check", result.stderr)

    def test_verify_cleanup_preserves_existing_files_on_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            tracked = repo / "__pycache__" / "fixture"
            tracked.parent.mkdir()
            tracked.write_text("keep")
            _git(repo, "add", "-f", "__pycache__/fixture")
            _git(repo, "commit", "-m", "fixture")
            existing = repo / "setup-input"
            existing.write_text("keep")
            for suffix, success in [("", True), ("; echo bad > __pycache__/fixture; exit 2", False)]:
                result = ad.run_verify_gate(
                    repo, "test -f setup-input && touch generated.log" + suffix,
                    allow_no_verify=False, log_file=root / "verify.log", repo_key="verify-clean",
                )
                self.assertEqual(result["passed"], success, result)
                self.assertFalse((repo / "generated.log").exists())
                self.assertEqual(tracked.read_text(), "keep")
                self.assertEqual(existing.read_text(), "keep")

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

    def test_utf16_credentials_block_untracked_staged_and_history_but_allow_removal(self) -> None:
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
                    base = _git(repo, "rev-parse", "HEAD")
                    original = "\ufeff项目\r\nsetting="
                    token = "lin_api_" + "SYNTHETIC" * 3
                    file.write_bytes((original + token + "\r\n结尾").encode(encoding))
                    self.assertTrue(ad._scan_before_git_write(repo))
                    _git(repo, "add", "config.txt")
                    self.assertTrue(ad.scan_staged_for_secrets(repo))
                    _git(repo, "commit", "-m", "introduce synthetic fixture")
                    introduced = _git(repo, "rev-parse", "HEAD")
                    self.assertTrue(ad.scan_range_for_secrets(repo, base, introduced))
                    file.write_bytes((original + "empty\r\n结尾").encode(encoding))
                    _git(repo, "commit", "-am", "remove synthetic fixture")
                    self.assertFalse(ad.scan_range_for_secrets(repo, introduced, "HEAD"))
                    self.assertTrue(ad.scan_range_for_secrets(repo, base, "HEAD"))
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

    def test_baseline_cache_uses_detected_recipe_on_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.invalid")
            (repo / "pyproject.toml").touch()
            (repo / "tests").mkdir()
            (repo / "tests/test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "commit", "--allow-empty", "-m", "feature")
            args = (repo, "r", repo, base, None, root / "run")
            with mock.patch.object(ad.shutil, "which", return_value=None):
                first = ad.check_base_baseline(*args)
                self.assertTrue(first["passed"])
                self.assertIn("unittest", first["command"])
                self.assertTrue(ad.check_base_baseline(*args)["cached"])
            _write_exec(root / "pytest", "#!/bin/sh\necho newly-selected-pytest >&2\nexit 1\n")
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}), mock.patch.object(
                ad.shutil, "which", side_effect=lambda name: str(root / "pytest") if name == "pytest" else None
            ):
                changed = ad.check_base_baseline(*args)
            self.assertFalse(changed["cached"])
            self.assertFalse(changed["passed"])
            self.assertIn("newly-selected-pytest", changed["output"])

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
                issue_pr = ad.fetch_open_pr_for_branch(d, "test/repo", "feat/x")
                self.assertEqual(issue_pr["number"], 7)
                fb = ad.fetch_open_pr_feedback(d, "test/repo", issue_pr["number"]) or ""
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

    def test_truncation_preserves_pem_redaction_and_bounds_unterminated_lines(self) -> None:
        # Make the opening delimiter land in the discarded middle of the raw display buffer.
        padding = "ordinary diagnostic\n" * 20
        payload = padding + "-----BEGIN PRIVATE KEY-----\n" + "synthetic-pem-body\n" * 20 + "-----END PRIVATE KEY-----\nfinished\n"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "verify.log"
            with mock.patch.object(ad, "DISPLAY_OUTPUT_CAP", 256):
                result = ad.run_cmd([sys.executable, "-B", "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                                    stdin_text=payload, stdout_path=path)
            for output in (path.read_text(), result.stdout):
                self.assertNotIn("synthetic-pem-body", output)
                self.assertIn("finished", output)
            with contextlib.ExitStack() as stack, mock.patch.object(ad._RedactedLog, "line_limit", 64):
                sink = ad._RedactedLog(path, stack)
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
            with contextlib.ExitStack() as stack, mock.patch.object(ad._RedactedLog, "line_limit", 64):
                sink = ad._RedactedLog(path, stack)
                sink.write(b"x" * 70 + b"-----BEGIN CERTIFICATE-----\nkept after cert\n")
                sink.write(b'{"type":"tool","out":"' + b"y" * 70 + b'-----BEGIN PRIVATE KEY-----\\nsecret"}\n')
                sink.write(b'{"type":"thread.started","thread_id":"aaaa-bbbb"}\n')
                sink.write(b"")
            logged = path.read_text()
            self.assertIn("kept after cert", logged)
            self.assertIn("aaaa-bbbb", logged)

    def test_polyglot_python_tests_dir_does_not_block_frontend_lint_and_setup_files_edge_cases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1. Polyglot repo: frontend package.json has lint (no JS tests) + Python tests/test_api.py
            (root / "package.json").write_text('{"scripts":{"lint":"eslint ."}}', encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_api.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            cmd = ad.detect_verify_command(root, None)
            self.assertEqual(cmd, "npm run lint && python3 -m unittest discover -s tests")

            # 2. Setup-generated .pem (e.g. non-gitignored venv/cacert.pem) excluded from secret scan/commit,
            #    and worker/verify deleting or touching setup files does not crash or falsely blame reviewer.
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
            cand, hits = ad.orchestrator_commit(repo, "TST-SET", "Work", 1, setup_paths=tuple(setup_files))
            self.assertFalse((repo / "cache.tmp").exists())
            self.assertEqual(hits, [])
            self.assertNotIn("venv_cacert.pem", _git(repo, "ls-tree", "-r", "--name-only", cand).splitlines())

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

    def test_oversized_harness_output_stops_and_keeps_session_trace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "codex"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' '\"session id: 11111111-2222-3333-4444\"' >&2\n"
                            "exec head -c 262144 /dev/zero\n")
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.defpath}), \
                    mock.patch.object(ad, "HARNESS_OUTPUT_LIMIT", 32768, create=True):
                with self.assertRaisesRegex(RuntimeError, "output limit"):
                    ad.invoke_harness(ad.parse_harness_spec("codex:sol"), "review", worktree=root, run_dir=root,
                                      schema=ad.REVIEW_SCHEMA, schema_file=root / "schema", prompt_file=root / "prompt",
                                      out_file=root / "result", read_only=True)
            end = json.loads((root / "sessions.jsonl").read_text().splitlines()[-1])
            self.assertEqual(end["status"], "error")
            self.assertEqual(end["session_id"], "11111111-2222-3333-4444")
            self.assertFalse((root / "result").exists())

    def test_oversized_broken_json_hides_following_plain_pem_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "verify.log"
            with contextlib.ExitStack() as stack:
                sink = ad._RedactedLog(log, stack)
                sink.write(b'{"error":"' + b"x" * sink.line_limit)
                sink.write(b"-----BEGIN PRIV")
                sink.write(b"ATE KEY-----\n")
                sink.write(b"onlySyntheticBodyData\n-----END PRIVATE KEY-----\nsafe diagnostic\n")
            self.assertNotIn("onlySyntheticBodyData", log.read_text())
            self.assertIn("safe diagnostic", log.read_text())

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

    def test_wildcard_touches_reserve_all_matching_descendants(self) -> None:
        for pattern, expected in [("**/shared", set()), ("*.ts", set()), ("src/*/utils", {"src"}),
                                  ("src/api/[av]*.py", {"src/api"}), ("src/user?.py", {"src"})]:
            with self.subTest(pattern=pattern):
                self.assertEqual(ad.parse_touches(f"## Touches\n- {pattern}"), expected)
        for root in ("*", "**", "`*`"):
            self.assertEqual(ad.parse_touches(f"## Touches\n- {root}\n- docs"), set())
        candidates = [{"identifier": "A", "description": "## Touches\n- src/*.py"},
                      {"identifier": "B", "description": "## Touches\n- src/core.py"}]
        selected, deferred = ad.select_non_overlapping_wave(candidates)
        self.assertEqual(selected, candidates[:1])
        self.assertEqual([entry["id"] for entry in deferred], ["B"])

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

    def test_touches_numbered_lists_and_fail_closed_unparseable_entries(self) -> None:
        self.assertEqual(ad.parse_touches("## Touches\n1. `src/auth`\n"), {"src/auth"})
        self.assertEqual(ad.parse_touches("## Touches\n2) src/auth\n10. `./src/routes/`\n"), {"src/auth", "src/routes"})
        self.assertEqual(ad.parse_touches("## Touches\n- src/auth\n"), ad.parse_touches("## Touches\n1. `src/auth`\n"))

        # Unparseable / non-literal entries fail closed as unscoped (empty set -> exclusive repo lock).
        for bad_entry in (
            "1. `src/auth` (login flow)",
            "- Touch auth and billing modules",
            "- • src/auth",
            "- (a) src/auth",
            "- src/{auth,billing}",
            "- `src/auth",
            "1. 仅修改登录模块",
        ):
            with self.subTest(bad_entry=bad_entry):
                self.assertEqual(ad.parse_touches(f"## Touches\n{bad_entry}\n"), set())

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
