"""Public contracts for the Agent Brief and Linear domain adapters."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from io import BytesIO
from unittest import mock

from agent_dispatch import brief, config, linear
from tests.helpers import FakeLinearState, IsolatedDispatchCase, _git, start_fake_linear


class TestBrief(IsolatedDispatchCase):
    def test_authorization_requires_real_heading_and_matching_fence(self) -> None:
        description = (
            "````md\n## Authorized Gate/Test Changes\n- gate: Makefile\n```\n"
            "- gate: pytest.ini\n````\n## Authorized Gate/Test Changes\n"
            "    - gate: setup.py\n~~~\n```\n- gate: setup.cfg\n~~~\n- gate: src/Makefile\n"
            "# Out of scope\n- gate: Cargo.toml\n"
        )
        self.assertEqual(brief.parse_authorized_changes(description), (["src/Makefile"], []))

    def test_adr_frontmatter_controls_active_status(self) -> None:
        self.assertTrue(brief.is_active_adr("---\nstatus: accepted # accepted | deprecated | superseded by ADR-0002\n---\n# Ok"))
        self.assertFalse(brief.is_active_adr("---\nstatus: superseded by ADR-0005\n---\n# Old"))
        self.assertFalse(brief.is_active_adr("---\nstatus: deprecated\n---\n# Old"))

    def test_touches_empty_and_comma_separated_entries(self) -> None:
        self.assertEqual(brief.parse_touches("## Touches\n- n/a\n"), set())
        self.assertEqual(brief.parse_touches("## Touches\n- src/routes, auth\n"), {"src/routes", "auth"})

    def test_authorization_ignores_free_form_text_and_broad_patterns(self) -> None:
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
                brief.parse_authorized_changes(f"## Authorized Gate/Test Changes\n{prose}\n"),
                ([], []),
                f"Expected fail-closed empty auth for {prose!r}",
            )
        self.assertEqual(
            brief.parse_authorized_changes("## Authorized Gate/Test Changes\n- gate: `pytest.ini`\n- test: tests/legacy_*.py\n"),
            (["pytest.ini"], ["tests/legacy_*.py"]),
        )

    def test_frontier_requires_agent_ready_label(self) -> None:
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
        ok_f, reason_f = brief.is_ready_for_frontier(unlabeled)
        self.assertFalse(ok_f)
        self.assertEqual(reason_f, "missing_agent_ready_label")

    def test_authorization_globs_match_path_segments(self) -> None:
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
        g_pats, t_pats = brief.parse_authorized_changes(fenced_brief)
        self.assertEqual(g_pats, [], "Fenced/quoted/wildcard gate patterns MUST be rejected!")
        self.assertEqual(t_pats, ["tests/*.py"])
        self.assertTrue(brief.path_matches_any_pattern("tests/test_a.py", t_pats))
        self.assertFalse(brief.path_matches_any_pattern("tests/nested/test_a.py", t_pats), "Single * MUST NOT cross /!")

    def test_frontier_requires_todo_state(self) -> None:
        custom_unstarted = {
            "identifier": "TST-WAIT",
            "title": "Needs approval",
            "state": {"name": "Awaiting approval", "type": "unstarted"},
            "assignee": None,
            "labels": {"nodes": [{"name": "agent-ready"}]},
            "children": {"nodes": []},
        }
        self.assertFalse(brief.is_ready_for_frontier(custom_unstarted)[0])

    def test_wildcard_touches_reserve_all_matching_descendants(self) -> None:
        for pattern, expected in [("**/shared", set()), ("*.ts", set()), ("src/*/utils", {"src"}),
                                  ("src/api/[av]*.py", {"src/api"}), ("src/user?.py", {"src"})]:
            with self.subTest(pattern=pattern):
                self.assertEqual(brief.parse_touches(f"## Touches\n- {pattern}"), expected)
        for root in ("*", "**", "`*`"):
            self.assertEqual(brief.parse_touches(f"## Touches\n- {root}\n- docs"), set())

    def test_touches_normalization_fences_headings_and_wildcards(self) -> None:
        description = (
            "```md\n## Touches\n- fake/from-codeblock\n```\n"
            "## Touches\n"
            "- `./src//api/`, `pkg\\core/*`\n"
            "# Appendix\n"
            "- unrelated/outside-section\n"
        )
        self.assertEqual(brief.parse_touches(description), {"src/api", "pkg/core"})
        self.assertTrue(brief.touches_overlap(brief.parse_touches("## Touches\n- ./src/api\n"), {"src/api/v1"}))
        # Wildcard root fails closed as unscoped empty set
        self.assertEqual(brief.parse_touches("## Touches\n- *\n"), set())
        self.assertEqual(brief.parse_touches("## Touches\n- **/*\n"), set())

    def test_touches_numbered_lists_and_fail_closed_unparseable_entries(self) -> None:
        self.assertEqual(brief.parse_touches("## Touches\n1. `src/auth`\n"), {"src/auth"})
        self.assertEqual(brief.parse_touches("## Touches\n2) src/auth\n10. `./src/routes/`\n"), {"src/auth", "src/routes"})
        self.assertEqual(brief.parse_touches("## Touches\n- src/auth\n"), brief.parse_touches("## Touches\n1. `src/auth`\n"))

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
                self.assertEqual(brief.parse_touches(f"## Touches\n{bad_entry}\n"), set())

    def test_baseline_snapshot_redacts_credentials(self) -> None:
        d = self.sandbox
        secret = "lin_api_" + "syntheticvalue123456"
        _git(d, "init", "-b", "main")
        (d / "GLOSSARY.md").write_text(secret)
        _git(d, "add", "GLOSSARY.md")
        _git(d, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "baseline")
        brief.snapshot_baseline_standards(d, "HEAD", d / "standards")
        self.assertNotIn(secret, (d / "standards" / "GLOSSARY.md").read_text())

    def test_baseline_snapshot_uses_active_adrs_from_base_and_checks_integrity(self) -> None:
        repo = self.sandbox
        _git(repo, "init", "-b", "main")
        adr_dir = repo / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        active = "---\nstatus: accepted\n---\n# 中文 ADR\n"
        (adr_dir / "中文.md").write_text(active, encoding="utf-8")
        (adr_dir / "old.md").write_text("---\nstatus: superseded by ADR-0005\n---\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "baseline")
        base_sha = _git(repo, "rev-parse", "HEAD")
        (adr_dir / "中文.md").write_text("# Working tree edit\n", encoding="utf-8")

        std_dir = repo / "standards"
        rel = "docs/adr/中文.md"
        files, hashes = brief.snapshot_baseline_standards(repo, base_sha, std_dir)
        self.assertEqual(files, [rel])
        self.assertEqual(hashes, {rel: hashlib.sha256(active.encode("utf-8")).hexdigest()})
        self.assertFalse((std_dir / "docs/adr/old.md").exists())

        snapshot = std_dir / rel
        self.assertEqual(snapshot.read_text(encoding="utf-8"), active)
        brief.assert_sha256_intact(snapshot, hashes[rel], rel)
        with mock.patch.object(os, "replace", side_effect=OSError("simulated replacement failure")):
            with self.assertRaisesRegex(OSError, "replacement failure"):
                brief.snapshot_baseline_standards(repo, base_sha, std_dir)
        brief.assert_sha256_intact(snapshot, hashes[rel], rel)

        snapshot.write_text("# Modified snapshot\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "integrity violation.*中文"):
            brief.assert_sha256_intact(snapshot, hashes[rel], rel)
        snapshot.unlink()
        with self.assertRaisesRegex(RuntimeError, "integrity violation.*中文"):
            brief.assert_sha256_intact(snapshot, hashes[rel], rel)

    def test_classify_blockers_ignores_completed_and_unrelated_issues(self) -> None:
        relations = [
            ("blocks", "TST-2", "started"),
            ("blocks", "TST-3", "canceled"),
            ("blocks", "TST-1", "unstarted"),
            ("blocks", "TST-2", "started"),
            ("blocks", "TST-4", "completed"),
            ("related", "TST-5", "unstarted"),
        ]
        issue = {"inverseRelations": {"nodes": [
            {"type": kind, "issue": {"identifier": identifier, "state": {"type": state}}}
            for kind, identifier, state in relations
        ]}}
        self.assertEqual(brief.classify_blockers(issue), (["TST-1", "TST-2"], ["TST-3"]))

    def test_issue_snapshot_marks_parent_context_and_redacts_human_comments(self) -> None:
        secret = "lin_api_" + "syntheticvalue123456"
        issue = {
            "identifier": "TST-1",
            "description": "Implement the ticket.",
            "parent": {"identifier": "TST-0", "title": "Parent", "description": "Context to read."},
            "comments": {"nodes": [
                {"body": "[agent-dispatch] Automation comment"},
                {"body": f"Human comment with {secret}", "user": {"name": "Teammate"}},
            ]},
        }
        snapshot = brief.render_issue_snapshot(issue)
        self.assertIn("## Ticket Description (Authoritative Acceptance Scope)\nImplement the ticket.", snapshot)
        self.assertIn("## Parent Spec (TST-0: Parent) — Context ONLY (NOT acceptance criteria for this slice)", snapshot)
        self.assertIn("Context to read.", snapshot)
        self.assertIn("Human comment with <REDACTED_SECRET>", snapshot)
        self.assertNotIn(secret, snapshot)
        self.assertNotIn("Automation comment", snapshot)


class TestLinear(IsolatedDispatchCase):
    def test_linear_auth_normalizes_personal_key_and_oauth(self) -> None:
        key = "lin_api_" + "X" * 24
        for value, expected in [(key, key), ("Bearer " + key, key), ("<test-oauth>", "Bearer <test-oauth>")]:
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": value}):
                self.assertEqual(linear.resolve_linear_auth(), expected)

    def test_linear_auth_reads_current_mcp_config_path(self) -> None:
        config_path = self.sandbox / "mcp.json"
        config_path.write_text(json.dumps({
            "mcpServers": {"linear": {"headers": {"Authorization": "<test-mcp-oauth>"}}},
        }), encoding="utf-8")
        with (
            mock.patch.dict(os.environ, {"LINEAR_API_KEY": ""}),
            mock.patch.object(config, "MCP_CONFIG_PATH", config_path),
            mock.patch.object(linear, "Path", return_value=self.sandbox / "etc-environment"),
        ):
            self.assertEqual(linear.resolve_linear_auth(), "Bearer <test-mcp-oauth>")

    def test_linear_transient_reads_and_credential_redirect_policy(self) -> None:
        responses = [
            {"errors": [{"extensions": {"code": "RATELIMITED"}}]},
            {"data": {"ok": True}},
        ]

        def boundary(request, timeout):
            self.assertEqual(request.get_header("Authorization"), "synthetic-test-credential")
            self.assertNotIn("Authorization", request.headers)  # urllib only copies these headers on redirect
            return BytesIO(json.dumps(responses.pop(0)).encode())

        with mock.patch.object(urllib.request, "urlopen", side_effect=boundary), mock.patch.object(time, "sleep"):
            self.assertEqual(linear.LinearClient("synthetic-test-credential").query("query { ok }"), {"ok": True})
        self.assertFalse(responses)

    def test_linear_query_retries_timeout(self) -> None:
        client = linear.LinearClient("synthetic-test-credential")
        responses = [TimeoutError("simulated read timeout"), BytesIO(b'{"data": {"ok": true}}')]
        with mock.patch.object(urllib.request, "urlopen", side_effect=responses) as request, mock.patch.object(time, "sleep"):
            result = client.query("query { ok }")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.call_count, 2)

    def test_project_issue_labels_are_paginated(self) -> None:
        client = linear.LinearClient("synthetic-test-credential")
        row = {"id": "one", "labels": {"nodes": [], "pageInfo": {"hasNextPage": True}}}
        responses = [
            {"project": {"issues": {"nodes": [row], "pageInfo": {"hasNextPage": False}}}},
            {"issue": {"labels": {"nodes": [{"id": "a", "name": "A"}], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}},
            {"issue": {"labels": {"nodes": [{"id": "b", "name": "B"}], "pageInfo": {"hasNextPage": False}}}},
        ]
        with mock.patch.object(urllib.request, "urlopen", side_effect=[
            BytesIO(json.dumps({"data": page}).encode()) for page in responses
        ]) as request:
            issues = client.list_project_issues("project-one")
        self.assertEqual([x["id"] for x in issues[0]["labels"]["nodes"]], ["a", "b"])
        self.assertEqual(json.loads(request.call_args.args[0].data)["variables"]["after"], "next")

    def test_atomic_label_changes_preserve_concurrent_human_labels(self) -> None:
        client = linear.LinearClient("synthetic-test-credential")
        labels = {"human-A"}

        def boundary(request, timeout):
            payload = json.loads(request.data)
            query, variables = payload["query"], payload["variables"]
            if "issueAddLabel(" in query:
                labels.add("concurrent-human")
                labels.add(variables["labelId"])
                data = {"issueAddLabel": {"success": True}}
            elif "issueRemoveLabel(" in query:
                labels.remove(variables["labelId"])
                data = {"issueRemoveLabel": {"success": True}}
            elif "labels(first:" in query:
                data = {"issue": {"labels": {
                    "nodes": [{"id": label, "name": label} for label in labels],
                    "pageInfo": {"hasNextPage": False},
                }}}
            else:
                self.fail("unexpected label operation")
            return BytesIO(json.dumps({"data": data}).encode())

        issue = {"id": "one", "team": {"labels": {"nodes": [{"id": "dispatch:failed", "name": "dispatch:failed"}]}}}
        with mock.patch.object(urllib.request, "urlopen", side_effect=boundary):
            client.ensure_label_and_add(issue, "dispatch:failed")
            client.remove_labels_if_present(issue, {"dispatch:failed"})
        self.assertEqual(labels, {"human-A", "concurrent-human"})

    def test_state_update_rejects_mismatched_state(self) -> None:
        client = linear.LinearClient("synthetic-test-credential")
        issue = {"id": "one", "identifier": "TST-1", "state": {"id": "todo"}}
        response = BytesIO(b'{"data": {"issueUpdate": {"success": true, "issue": {"state": {"id": "wrong"}}}}}')
        with mock.patch.object(urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "issueUpdate failed verification"):
                client.update_issue_state_by_id(issue, "started")
        self.assertEqual(issue["state"], {"id": "todo"})

    def test_state_update_does_not_retry_timeout(self) -> None:
        client = linear.LinearClient("synthetic-test-credential")
        issue = {"id": "one", "identifier": "TST-1", "state": {"id": "todo"}}
        with mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError) as request, mock.patch.object(time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Linear network error"):
                client.update_issue_state_by_id(issue, "started")
        request.assert_called_once()

    def test_resolve_project_uses_remote_marker_and_explicit_argument(self) -> None:
        repo = self.sandbox
        _git(repo, "init", "-b", "main")
        _git(repo, "remote", "add", "origin", "https://github.com/example/demo-repo.git")
        server, url = start_fake_linear(FakeLinearState("Demo Repo"))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = linear.LinearClient("synthetic-test-credential", url=url)

        self.assertEqual(linear.resolve_project(client, repo)["name"], "Demo Repo")
        marker = repo / ".linear-project"
        marker.write_text("proj-1\n", encoding="utf-8")
        self.assertEqual(linear.resolve_project(client, repo)["id"], "proj-1")
        with self.assertRaisesRegex(RuntimeError, "Explicit Linear project 'missing-project' from --project not found"):
            linear.resolve_project(client, repo, explicit="missing-project")
