#!/usr/bin/env python3
"""
Unit, security/false-positive, end-to-end CLI fault-injection, and opt-in live Linear contract tests
for agent-dispatch v3 and install.sh.
"""

from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

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
    return subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=check).stdout.strip()


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
                    "labels": {"nodes": i["_labels"]},
                    "children": {"nodes": i["_children"]},
                }
                for i in uniq
            ]
            return {"data": {"project": {"id": self.project["id"], "name": self.project["name"], "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
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
            if "addedLabelIds" in variables:
                for lid in variables["addedLabelIds"]:
                    lbl = next(l for l in self.team_labels if l["id"] == lid)
                    if all(x["id"] != lid for x in iss["_labels"]):
                        iss["_labels"].append(lbl)
                self._tick += 1
                iss["updatedAt"] = f"2026-11-24T00:00:{self._tick:02d}Z"
                self.mutation_log.append(("addLabels", {"id": iss["identifier"], "labels": [l["name"] for l in iss["_labels"]]}))
                return {"data": {"issueUpdate": {"success": True, "issue": {"updatedAt": iss["updatedAt"], "labels": {"nodes": iss["_labels"]}}}}}
            if "removedLabelIds" in variables:
                rem = set(variables["removedLabelIds"])
                iss["_labels"] = [l for l in iss["_labels"] if l["id"] not in rem]
                self._tick += 1
                iss["updatedAt"] = f"2026-11-24T00:00:{self._tick:02d}Z"
                self.mutation_log.append(("removeLabels", {"id": iss["identifier"], "labels": [l["name"] for l in iss["_labels"]]}))
                return {"data": {"issueUpdate": {"success": True, "issue": {"updatedAt": iss["updatedAt"], "labels": {"nodes": iss["_labels"]}}}}}
            if "labelIds" in variables:
                iss["_labels"] = [l for l in self.team_labels if l["id"] in variables["labelIds"]]
                self._tick += 1
                iss["updatedAt"] = f"2026-11-24T00:00:{self._tick:02d}Z"
                self.mutation_log.append(("setLabels", {"id": iss["identifier"], "labels": [l["name"] for l in iss["_labels"]]}))
                return {"data": {"issueUpdate": {"success": True, "issue": {"updatedAt": iss["updatedAt"], "labels": {"nodes": iss["_labels"]}}}}}
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
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
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

class TestUnitGatesAndParsing(unittest.TestCase):
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
            "AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGqeTA",
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

    def test_justfile_and_makefile_variable_assignments_not_misdetected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "Makefile").write_text("verify := echo fake\ncheck-all:\n\techo nope\n", encoding="utf-8")
            self.assertIsNone(ad.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\ncheck-all:\n    echo nope\n', encoding="utf-8")
            self.assertIsNone(ad.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\nverify:\n    pytest\n', encoding="utf-8")
            self.assertEqual(ad.detect_verify_command(d, None), "just verify")

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
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{"number": 7, "url": "u", "isDraft": True}]), stderr="")
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
                non_draft = {"number": 5, "url": "u", "isDraft": False, "baseRefName": "main", "headRefOid": "0" * 40}
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

                def interrupt_during_publish(c: Any, i: Any, wt: Path, rd: Path, lf: Path, st: dict[str, Any]) -> None:
                    st["push_attempted"] = True
                    st["stage"] = "pushing"
                    ad.save_checkpoint(rd, lf, st)
                    raise KeyboardInterrupt("Simulated SIGINT mid-push")

                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CACHE_ROOT": str(cache_dir)}):
                    with mock.patch.object(ad, "invoke_harness", side_effect=fake_worker_invoke):
                        with mock.patch.object(ad, "reconcile_publish", side_effect=interrupt_during_publish):
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
                rkey = ad.repo_cache_key(repo)
                ckpt = json.loads((cache_dir / "runs" / rkey / "TST-SIG" / "latest.json").read_text(encoding="utf-8"))
                self.assertEqual(ckpt["stage"], "pushing", "Publish-phase signal MUST NOT overwrite stage to 'error'!")
                self.assertTrue(ckpt["push_attempted"])
                # Linear MUST NOT have been rolled back to Todo, and MUST carry dispatch:publish-incomplete!
                self.assertEqual(iss["state"]["name"], "In Progress")
                self.assertIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
                self.assertNotIn("dispatch:failed", [l["name"] for l in iss["_labels"]])
            finally:
                srv.shutdown()
                srv.server_close()


# ---------------------------------------------------------------------------
# End-to-End Subprocess CLI Fault-Injection Suite
# ---------------------------------------------------------------------------

class TestEndToEndCLIFaultInjection(unittest.TestCase):
    def setUp(self) -> None:
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
import json, os, sys
from pathlib import Path

assert os.environ.get("AGENT_DISPATCH_CHILD") == "1", "Missing anti-nesting env"
assert "LINEAR_API_KEY" not in os.environ, "LINEAR_API_KEY leaked to codex!"
assert "GH_TOKEN" not in os.environ, "GH_TOKEN leaked to codex!"
assert "--trust" not in sys.argv, "--trust must not be passed (mutates global config.toml)!"
assert "mcp_servers.linear.enabled=false" in sys.argv, "Linear MCP must be disabled in headless codex!"

# Parse -o <out_file> and -C <cwd>
args = sys.argv[1:]
out_file = Path(args[args.index("-o") + 1])
cwd = Path(args[args.index("-C") + 1])
mode = os.environ.get("FAKE_CODEX_MODE", "good")
counter_file = Path("{self.root}/codex_calls.count")
calls = int(counter_file.read_text()) + 1 if counter_file.exists() else 1
counter_file.write_text(str(calls))

print(f"session id: 11111111-2222-3333-4444-{{calls:012d}}", file=sys.stderr)

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
out_file.write_text(json.dumps(res), encoding="utf-8")
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
import json, subprocess, sys
from pathlib import Path

db = Path("{self.gh_state_file}")
calls_log = Path("{self.gh_calls_log}")
prs = json.loads(db.read_text())
args = sys.argv[1:]
with calls_log.open("a", encoding="utf-8") as fh:
    fh.write(" ".join(args[:4]) + "\\n")

if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1] if "--head" in args else ""
    state = args[args.index("--state") + 1] if "--state" in args else "open"
    matched = [p for p in prs if p["head"] == head and p["state"].lower() == state.lower()]
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
    body = args[args.index("--body") + 1]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
    pr = {{
        "number": len(prs) + 1,
        "url": f"https://github.com/test/demo-repo/pull/{{len(prs)+1}}",
        "head": head,
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
sys.exit(0)
""",
        )

    def _run_cli(self, cli_args: list[str], *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
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

        # 2. Secret in Linear ticket title MUST be blocked BEFORE commit/push!
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

        # 3. Human cancels ticket mid-run -> finally MUST preserve Canceled (NEVER overwrite back to Todo)!
        iss_can = self.linear_state.add_issue(
            "TST-42",
            "Cancel mid-run",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        # Wait for claim -> flip state to Canceled before worker exits by hooking FakeLinearState via a quick in-process helper
        canceled_st = next(s for s in self.linear_state.states if s["name"] == "Canceled")
        orig_handle = self.linear_state.handle

        def cancel_after_claim(q: str, v: dict[str, Any]) -> dict[str, Any]:
            resp = orig_handle(q, v)
            if "mutation" in q and "issueUpdate" in q and v.get("id") == iss_can["id"] and v.get("stateId") == "st-prog":
                # Simulate human canceling the ticket right after worker claims it!
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

        # 4. CLI rejects non-positive --max-parallel and --limit!
        bad_par = self._run_cli(["frontier", "--max-parallel", "0", "--repo", str(self.repo)])
        self.assertEqual(bad_par.returncode, 2)
        bad_lim = self._run_cli(["frontier", "--limit", "-1", "--repo", str(self.repo)])
        self.assertEqual(bad_lim.returncode, 2)

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
            ag_skills = d / "agent_skills"
            cx_skills = d / "codex_skills"
            cx_agents = d / "codex" / "AGENTS.md"
            bin_dst = d / "bin" / "agent-dispatch"
            (ag_skills / "my-custom-skill").mkdir(parents=True)
            (ag_skills / "my-custom-skill" / "SKILL.md").write_text("keep me", encoding="utf-8")
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
            subprocess.run([str(INSTALL_SH)], check=True, env=env, capture_output=True)
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


# ---------------------------------------------------------------------------
# Opt-in Live Read-Only Linear GraphQL Contract Tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.environ.get("RUN_LIVE_LINEAR_TESTS") == "1", "Set RUN_LIVE_LINEAR_TESTS=1 for live Linear checks")
class TestLiveLinearReadOnlyContract(unittest.TestCase):
    def test_live_linear_project_string_query_and_pagination(self) -> None:
        auth = ad.resolve_linear_auth()
        client = ad.LinearClient(auth)
        teams, projects = client.list_teams_and_projects()
        self.assertTrue(len(teams) >= 1)
        self.assertTrue(len(projects) >= 1)
        issues = client.list_project_issues(projects[0]["id"], state_type="unstarted")
        self.assertIsInstance(issues, list)

    def test_explicit_project_typo_fails_fast_with_specific_message(self) -> None:
        client = ad.LinearClient(ad.resolve_linear_auth())
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "Explicit Linear project"):
                ad.resolve_project(client, Path(td), explicit="definitely-nonexistent-project-xyz")


if __name__ == "__main__":
    unittest.main()
