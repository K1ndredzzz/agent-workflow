"""Shared fixtures for agent-dispatch tests."""

from __future__ import annotations

import http.server
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

WORKFLOW_ROOT = Path(__file__).resolve().parent.parent


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


class IsolatedDispatchCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sandbox = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.sandbox),
            "PATH": os.defpath + ":/usr/local/bin",
            "LC_ALL": "C.UTF-8",
            "AGENT_DISPATCH_CACHE_ROOT": str(self.sandbox / "cache"),
            "AGENT_DISPATCH_LINEAR_URL": "http://127.0.0.1:1/graphql",
            "LINEAR_API_KEY": "synthetic-test-credential",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=True))
