#!/usr/bin/env python3
"""Unit, fault-injection, and live read-only Linear contract tests for agent-dispatch v2."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN_PATH = Path(__file__).resolve().parent.parent / "bin" / "agent-dispatch"
loader = importlib.machinery.SourceFileLoader("agent_dispatch", str(BIN_PATH))
spec = importlib.util.spec_from_loader("agent_dispatch", loader)
assert spec is not None
ad = importlib.util.module_from_spec(spec)
sys.modules["agent_dispatch"] = ad
loader.exec_module(ad)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True).stdout.strip()


class TestSchemasAndParsing(unittest.TestCase):
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

    def test_adr_frontmatter_parser_does_not_filter_accepted_with_comments(self) -> None:
        template_with_comment = "---\nstatus: accepted # accepted | deprecated | superseded by ADR-0002\n---\n# Decision\nReplaces deprecated API."
        self.assertTrue(ad.is_active_adr(template_with_comment))
        superseded = "---\nstatus: superseded by ADR-0005\n---\n# Old"
        self.assertFalse(ad.is_active_adr(superseded))
        deprecated = "---\nstatus: deprecated\n---\n# Old"
        self.assertFalse(ad.is_active_adr(deprecated))

    def test_pr_body_has_no_broken_dedent_indentation(self) -> None:
        body = ad.build_pr_body(
            {"identifier": "K1N-99", "title": "Add auth", "url": "https://linear.app/k1n/issue/K1N-99"},
            "a" * 40,
            ad.parse_harness_spec("codex:sol"),
            ad.parse_harness_spec("agy:neon"),
            "Implemented auth.",
            ["pytest"],
            {"ran": True, "command": "pytest", "passed": True},
            {
                "passed": True,
                "standards": {"verdict": "pass", "summary": "OK", "hard_findings": [], "suggestions": ["Rename x"]},
                "spec": {"verdict": "pass", "summary": "All criteria met", "hard_findings": [], "suggestions": []},
            },
            ["[attempt 1] Independent logging bug"],
        )
        for line in body.splitlines():
            self.assertFalse(line.startswith("    "), f"Unexpected 4-space codeblock indent in PR line: {line!r}")
        self.assertIn("## Two-Axis Code Review (Unmerged)", body)
        self.assertIn("### Standards Axis (`PASS`)", body)
        self.assertIn("[attempt 1] Independent logging bug", body)

    def test_secret_redaction_and_child_env_stripping(self) -> None:
        raw = "Bearer lin_api_ABCDEF1234567890 and gho_1234567890123456789012345"
        red = ad.redact_secrets(raw)
        self.assertNotIn("lin_api_ABCDEF", red)
        self.assertNotIn("gho_1234567890", red)

        os.environ["LINEAR_API_KEY"] = "lin_api_dummy_test_1234567890"
        stripped = ad.build_child_env(include_secrets=False)
        self.assertNotIn("LINEAR_API_KEY", stripped)
        self.assertNotIn("GH_TOKEN", stripped)


class TestGatesAndGitGuards(unittest.TestCase):
    def test_verify_gate_fails_closed_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            res = ad.run_verify_gate(Path(td), None, allow_no_verify=False, log_file=Path(td) / "v.log")
            self.assertFalse(res["passed"])
            res_ok = ad.run_verify_gate(Path(td), None, allow_no_verify=True, log_file=Path(td) / "v.log")
            self.assertTrue(res_ok["passed"])

    def test_protected_paths_and_test_integrity_diff_and_secret_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "test@example.com")
            _git(repo, "config", "user.name", "Test")
            (repo / "Makefile").write_text("verify:\n\techo ok\n", encoding="utf-8")
            (repo / "test_app.py").write_text("def test_one():\n    assert 1 == 1\n", encoding="utf-8")
            (repo / "docs" / "adr").mkdir(parents=True)
            (repo / "docs" / "adr" / "0001-arch.md").write_text("---\nstatus: accepted\n---\n# Arch\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "init")
            base_sha = _git(repo, "rev-parse", "HEAD")

            # Verify makefile detection ignores variable assignments like `verify := foo`
            self.assertEqual(ad.detect_verify_command(repo, None), "make verify")

            # Baseline standards snapshot reads immutable base_sha
            snap_dir = repo / ".tmp_snap"
            std_files = ad.snapshot_baseline_standards(repo, base_sha, snap_dir)
            self.assertIn("docs/adr/0001-arch.md", std_files)

            # Worker attempts to tamper with Makefile and add pytest.mark.skip
            (repo / "Makefile").write_text("verify:\n\ttrue\n", encoding="utf-8")
            (repo / "test_app.py").write_text("@pytest.mark.skip\ndef test_one():\n    assert 1 == 1\n", encoding="utf-8")
            _git(repo, "add", "Makefile", "test_app.py")
            _git(repo, "commit", "-m", "tamper")
            cand_sha = _git(repo, "rev-parse", "HEAD")

            prot = ad.check_protected_paths(repo, base_sha, cand_sha)
            self.assertTrue(any("Makefile" in x for x in prot))

            ti = ad.check_test_integrity_diff(repo, base_sha, cand_sha)
            self.assertTrue(any("skip" in x for x in ti))

            # Secret scan refuses staging .env
            (repo / ".env").write_text("LINEAR_API_KEY=lin_api_supersecret123456\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ad.orchestrator_commit(repo, "K1N-1", "test", 1)

    def test_definition_of_ready_and_blockers_and_touches(self) -> None:
        spec_parent = {"identifier": "K1N-1", "title": "Spec", "state": {"name": "Todo", "type": "unstarted"}, "labels": {"nodes": [{"name": "spec"}]}, "description": "## Acceptance Criteria\n- x"}
        self.assertFalse(ad.is_ready_for_frontier(spec_parent)[0])

        wayfinder_ticket = {"identifier": "K1N-2", "title": "[Research] Compare DBs", "state": {"name": "Todo", "type": "unstarted"}, "labels": {"nodes": [{"name": "agent-ready"}]}, "description": ""}
        self.assertFalse(ad.is_ready_for_frontier(wayfinder_ticket)[0])

        raw_note = {"identifier": "K1N-3", "title": "Fix thing", "state": {"name": "Todo", "type": "unstarted"}, "labels": {"nodes": []}, "description": "just fix it"}
        self.assertFalse(ad.is_ready_for_frontier(raw_note)[0])

        ready = {
            "identifier": "K1N-4",
            "title": "Add endpoint",
            "state": {"name": "Todo", "type": "unstarted"},
            "labels": {"nodes": []},
            "description": "## Touches\n- routes, auth\n\n## Testing Seam & Acceptance Criteria\n- [ ] works",
        }
        self.assertTrue(ad.is_ready_for_frontier(ready)[0])
        self.assertEqual(ad.parse_touches(ready["description"]), {"routes", "auth"})

        # Canceled blocker is surfaced distinctly and does NOT silently pass
        blocked_iss = {
            "inverseRelations": {
                "nodes": [
                    {"type": "blocks", "issue": {"identifier": "K1N-8", "state": {"type": "canceled"}}},
                    {"type": "blocks", "issue": {"identifier": "K1N-9", "state": {"type": "completed"}}},
                ]
            }
        }
        active, canceled = ad.classify_blockers(blocked_iss)
        self.assertEqual(active, [])
        self.assertEqual(canceled, ["K1N-8"])

    def test_nonblocking_lock_prevents_double_claim(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ticket.lock"
            with ad.file_lock(lp, blocking=False):
                self.assertTrue(ad.is_locked(lp))
                with self.assertRaises(RuntimeError):
                    with ad.file_lock(lp, blocking=False):
                        pass
            self.assertFalse(ad.is_locked(lp))


class TestLiveLinearReadOnlyContract(unittest.TestCase):
    def test_live_linear_project_string_query_and_pagination(self) -> None:
        auth = ad.resolve_linear_auth()
        client = ad.LinearClient(auth)
        teams, projects = client.list_teams_and_projects()
        self.assertTrue(len(teams) >= 1)
        self.assertTrue(len(projects) >= 1)
        # Exercises the exact fixed `project(id: String!)` paginated query
        issues = client.list_project_issues(projects[0]["id"], state_type="unstarted")
        self.assertIsInstance(issues, list)

    def test_explicit_project_typo_fails_fast_without_fallback(self) -> None:
        client = ad.LinearClient(ad.resolve_linear_auth())
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                ad.resolve_project(client, Path(td), explicit="definitely-nonexistent-project-xyz")

    def test_update_issue_state_refreshes_in_memory_state(self) -> None:
        class FakeClient(ad.LinearClient):
            def __init__(self) -> None:
                self.calls: list[str] = []

            def query(self, gql: str, variables=None, *, retry: bool = True):
                sid = variables["stateId"]
                self.calls.append(sid)
                name = "In Progress" if sid == "s_prog" else "In Review"
                return {"issueUpdate": {"success": True, "issue": {"id": "i1", "identifier": "K1N-1", "state": {"id": sid, "name": name, "type": "started"}}}}

        fc = FakeClient()
        issue = {
            "id": "i1",
            "identifier": "K1N-1",
            "state": {"id": "s_rev", "name": "In Review", "type": "started"},
            "team": {"states": {"nodes": [{"id": "s_prog", "name": "In Progress", "type": "started"}, {"id": "s_rev", "name": "In Review", "type": "started"}]}},
        }
        fc.update_issue_state(issue, "In Progress")
        self.assertEqual(issue["state"]["id"], "s_prog")
        # Rerun back to In Review MUST NOT skip mutation!
        fc.update_issue_state(issue, "In Review")
        self.assertEqual(fc.calls, ["s_prog", "s_rev"])
        self.assertEqual(issue["state"]["id"], "s_rev")


if __name__ == "__main__":
    unittest.main()
