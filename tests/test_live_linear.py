"""Opt-in read-only contract tests against the live Linear GraphQL API."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_dispatch.linear import LinearClient, resolve_linear_auth, resolve_project


@unittest.skipUnless(os.environ.get("RUN_LIVE_LINEAR_TESTS") == "1", "Set RUN_LIVE_LINEAR_TESTS=1 for live Linear checks")
class TestLiveLinearReadOnlyContract(unittest.TestCase):
    def setUp(self) -> None:
        # Pin the authorized origin and reject any accidental mutation before creating a request.
        self.client = LinearClient(resolve_linear_auth(), url="https://api.linear.app/graphql")
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
                resolve_project(self.client, Path(td), explicit="definitely-nonexistent-project-xyz")
