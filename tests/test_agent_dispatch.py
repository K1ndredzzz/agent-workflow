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
        # Must still catch real tokens
        self.assertEqual(ad.redact_secrets("key=sk-proj-1234567890abcdefghij1234"), "key=<REDACTED_SECRET>")

    def test_child_env_stripping_isolated_without_polluting_os_environ(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LINEAR_API_KEY": "lin_api_test1234567890",
                "GH_TOKEN": "gho_1234567890123456789012345",
                "GITHUB_TOKEN": "ghp_1234567890123456789012345",
                "CODEX_SUB2API_API_KEY": "sub2api_test_key",
            },
        ):
            child_env = ad.build_child_env(role="child")
            self.assertEqual(child_env["AGENT_DISPATCH_CHILD"], "1")
            self.assertEqual(child_env["GIT_TERMINAL_PROMPT"], "0")
            for k in ("LINEAR_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "CODEX_SUB2API_API_KEY"):
                self.assertNotIn(k, child_env)
            codex_env = ad.build_child_env(role="codex")
            self.assertIn("CODEX_SUB2API_API_KEY", codex_env)
            self.assertNotIn("LINEAR_API_KEY", codex_env)
            self.assertNotIn("GH_TOKEN", codex_env)

    def test_justfile_and_makefile_variable_assignments_not_misdetected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "Makefile").write_text("verify := echo fake\ncheck-all:\n\techo nope\n", encoding="utf-8")
            self.assertIsNone(ad.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\ncheck-all:\n    echo nope\n', encoding="utf-8")
            self.assertIsNone(ad.detect_verify_command(d, None))
            (d / "Justfile").write_text('verify := "nope"\nverify:\n    pytest\n', encoding="utf-8")
            self.assertEqual(ad.detect_verify_command(d, None), "just verify")

    def test_touches_wave_selection_and_authorized_gate_brief(self) -> None:
        self.assertEqual(ad.parse_touches("## Touches\n- n/a\n"), set())
        self.assertEqual(ad.parse_touches("## Touches\n- src/routes, auth\n"), {"src/routes", "auth"})
        self.assertFalse(ad.has_authorized_gate_or_test_changes("## Authorized Gate/Test Changes\n- None\n"))
        self.assertTrue(ad.has_authorized_gate_or_test_changes("## Authorized Gate/Test Changes\n- Migrate pytest.ini\n"))

        iss_a = {"identifier": "TST-1", "description": "## Touches\n- src/routes\n"}
        iss_b = {"identifier": "TST-2", "description": "## Touches\n- src/auth\n"}
        iss_c = {"identifier": "TST-3", "description": "## Touches\n- src/routes\n"}
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
        (self.repo / "test_app.py").write_text("from app import add\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
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

        # Fake gh shim recording PR state to self.gh_state_file
        _write_exec(
            self.fake_bin / "gh",
            f"""#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path

db = Path("{self.gh_state_file}")
prs = json.loads(db.read_text())
args = sys.argv[1:]
if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1] if "--head" in args else ""
    state = args[args.index("--state") + 1] if "--state" in args else "open"
    matched = [p for p in prs if p["head"] == head and p["state"] == state]
    print(json.dumps(matched))
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
        "state": "open",
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
        """Directly reproduces Opus & Astra's P0-1: step 4 comment failure MUST NOT roll back Linear to Todo, and resume converges cleanly!"""
        iss = self.linear_state.add_issue(
            "TST-10",
            "Add multiply",
            "## What to Build\nAdd mul(a, b)\n\n## Testing Seam & Acceptance Criteria\n- [ ] mul(2, 3) == 6\n",
            labels=["agent-ready"],
        )
        # Inject failure ONLY on step 4 (delivery summary commentCreate)
        self.linear_state.fail_next_comment_create = True

        res1 = self._run_cli(["run", "TST-10", "--repo", str(self.repo), "--skip-baseline"])
        self.assertEqual(res1.returncode, 1, res1.stderr + res1.stdout)
        out1 = json.loads(res1.stdout)
        self.assertEqual(out1["status"], "publish_incomplete")
        self.assertTrue(out1["pushed"])
        self.assertIn("https://github.com/test/demo-repo/pull/1", out1["pr_url"])
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
        # Linear is In Review and failure label was cleared!
        self.assertEqual(iss["state"]["name"], "In Review")
        self.assertNotIn("dispatch:publish-incomplete", [l["name"] for l in iss["_labels"]])
        # Re-running resume a second time is idempotent and does NOT duplicate summary comment!
        cmts_before = len(self.linear_state.comments[iss["id"]])
        res3 = self._run_cli(["resume", "TST-10", "--repo", str(self.repo)])
        self.assertEqual(res3.returncode, 0, res3.stderr)
        self.assertEqual(len(self.linear_state.comments[iss["id"]]), cmts_before)

    def test_e2e_protected_path_triggers_rework_then_passes_and_cleanup_protects_new_commits(self) -> None:
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

        # Simulate historical merged PR with OLD SHA, then add a new local commit on the branch -> cleanup MUST NOT delete it!
        prs = json.loads(self.gh_state_file.read_text())
        prs[0]["state"] = "merged"
        prs[0]["headRefOid"] = "0" * 40  # old merged SHA != current worktree HEAD
        self.gh_state_file.write_text(json.dumps(prs))

        wt = Path(out["worktree"])
        (wt / "notes.txt").write_text("new unpushed work\n", encoding="utf-8")
        _git(wt, "add", "notes.txt")
        _git(wt, "commit", "-m", "new unpushed commit")

        clean_res = self._run_cli(["cleanup", "--repo", str(self.repo)])
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr)
        clean_out = json.loads(clean_res.stdout)
        self.assertEqual(clean_out["removed"], [])
        self.assertTrue(wt.exists(), "cleanup wrongly deleted worktree with unpushed commits!")

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
