"""Linear authentication, GraphQL operations, and project resolution."""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_dispatch import config
from agent_dispatch.process import git_cmd, run_cmd, sha256_text
from agent_dispatch.secrets import redact_secrets, safe_json


def resolve_linear_auth() -> str:
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not key:
        env_file = Path("/etc/environment")
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("LINEAR_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        key = val
                        break
    if not key and config.MCP_CONFIG_PATH.is_file():
        try:
            cfg = json.loads(config.MCP_CONFIG_PATH.read_text(encoding="utf-8"))
            key = str(cfg.get("mcpServers", {}).get("linear", {}).get("headers", {}).get("Authorization") or "").strip()
        except Exception:
            pass
    if not key:
        raise RuntimeError("LINEAR_API_KEY not found in env, /etc/environment, or ~/.gemini/config/mcp_config.json")
    parts = key.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        key = parts[1]
    return key if key.startswith("lin_api_") else f"Bearer {key}"


class LinearClient:
    def __init__(self, auth_header: str, *, url: str | None = None) -> None:
        self.auth_header = auth_header
        self.url = url or config._linear_url()

    def query(self, gql: str, variables: dict[str, Any] | None = None, *, retry: bool = True) -> dict[str, Any]:
        payload = json.dumps({"query": gql, "variables": variables or {}}).encode("utf-8")
        attempts = 4 if retry else 1
        last_err: Exception | None = None
        for attempt in range(attempts):
            req = urllib.request.Request(
                self.url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            req.add_unredirected_header("Authorization", self.auth_header)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if body.get("errors"):
                    errors = body["errors"]
                    codes = {(err.get("extensions") or {}).get("code") for err in errors}
                    if codes <= {"RATELIMITED", "INTERNAL_SERVER_ERROR"} and attempt + 1 < attempts:
                        time.sleep(1.5 * (2**attempt))
                        continue
                    raise RuntimeError(f"Linear GraphQL errors: {safe_json(errors)}")
                return body["data"]
            except urllib.error.HTTPError as e:
                err_body = redact_secrets(e.read().decode("utf-8", errors="ignore"))
                if retry and e.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    time.sleep(1.5 * (2**attempt))
                    last_err = RuntimeError(f"Linear API HTTP {e.code}: {err_body[:400]}")
                    continue
                raise RuntimeError(f"Linear API HTTP {e.code}: {err_body[:500]}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionResetError, http.client.IncompleteRead, OSError) as e:
                if retry and attempt < attempts - 1:
                    time.sleep(1.5 * (2**attempt))
                    last_err = e
                    continue
                raise RuntimeError(f"Linear network error: {redact_secrets(str(e))}") from e
        raise RuntimeError(f"Linear query failed after retries: {redact_secrets(str(last_err))}")

    def _paginate_root(self, field: str, node_selection: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            data = self.query(
                f"""
                query($after: String) {{
                  {field}(first: 50, after: $after) {{
                    nodes {{ {node_selection} }}
                    pageInfo {{ hasNextPage endCursor }}
                  }}
                }}
                """,
                {"after": cursor},
            )
            conn = data[field]
            items.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                break
            nxt = conn["pageInfo"].get("endCursor")
            if not nxt or nxt in seen_cursors:
                raise RuntimeError(f"Linear pagination stalled on {field} (cursor={nxt!r})")
            seen_cursors.add(nxt)
            cursor = nxt
        return items

    def _paginate_entity_field(self, root_field: str, entity_id: str, field: str, node_selection: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            data = self.query(
                f"""
                query($id: String!, $after: String) {{
                  {root_field}(id: $id) {{
                    {field}(first: 50, after: $after) {{
                      nodes {{ {node_selection} }}
                      pageInfo {{ hasNextPage endCursor }}
                    }}
                  }}
                }}
                """,
                {"id": entity_id, "after": cursor},
            )
            ent = data.get(root_field)
            if not ent:
                raise RuntimeError(f"Linear {root_field} disappeared while reading {field}.")
            conn = ent[field]
            items.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                break
            nxt = conn["pageInfo"].get("endCursor")
            if not nxt or nxt in seen_cursors:
                raise RuntimeError(f"Linear pagination stalled on {root_field}.{field} (cursor={nxt!r})")
            seen_cursors.add(nxt)
            cursor = nxt
        return items

    def list_teams_and_projects(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        teams = self._paginate_root("teams", "id key name")
        for t in teams:
            t["states"] = {"nodes": self._paginate_entity_field("team", t["id"], "states", "id name type")}
        projects = self._paginate_root("projects", "id name state teams { nodes { id key name } }")
        return teams, projects

    def get_issue(self, identifier: str) -> dict[str, Any]:
        data = self.query(
            """
            query($id: String!) {
              issue(id: $id) {
                id
                identifier
                title
                description
                url
                priority
                updatedAt
                assignee { id name email }
                state { id name type }
                team { id key name }
                project { id name }
                parent { id identifier title description url }
              }
            }
            """,
            {"id": identifier},
        )
        issue = data.get("issue")
        if not issue:
            raise RuntimeError(f"Linear issue '{identifier}' not found.")

        issue_uuid = issue["id"]
        team_id = issue["team"]["id"]
        issue["team"]["states"] = {"nodes": self._paginate_entity_field("team", team_id, "states", "id name type")}
        issue["team"]["labels"] = {"nodes": self._paginate_entity_field("team", team_id, "labels", "id name")}
        issue["labels"] = {"nodes": self._paginate_entity_field("issue", issue_uuid, "labels", "id name")}
        comments = self._paginate_entity_field("issue", issue_uuid, "comments", "id body createdAt user { name }")
        inv_rels = self._paginate_entity_field(
            "issue", issue_uuid, "inverseRelations", "type issue { id identifier title state { name type } }"
        )
        children = self._paginate_entity_field(
            "issue",
            issue_uuid,
            "children",
            "id identifier title priority description assignee { id name } state { id name type } labels { nodes { id name } pageInfo { hasNextPage } } children(first: 1) { nodes { id } }",
        )
        issue["comments"] = {"nodes": sorted(comments, key=lambda c: c.get("createdAt") or "")}
        issue["inverseRelations"] = {"nodes": inv_rels}
        self._complete_issue_labels(children)
        issue["children"] = {"nodes": children}
        return issue

    def _complete_issue_labels(self, issues: list[dict[str, Any]]) -> None:
        for issue in issues:
            if issue["labels"]["pageInfo"]["hasNextPage"]:
                issue["labels"] = {"nodes": self._paginate_entity_field("issue", issue["id"], "labels", "id name")}

    def get_issue_updated_at(self, issue_uuid: str) -> str:
        data = self.query("query($id: String!) { issue(id: $id) { updatedAt } }", {"id": issue_uuid})
        return str((data.get("issue") or {}).get("updatedAt") or "")

    def get_issue_ownership_snapshot(self, issue_uuid: str) -> dict[str, str]:
        """Fetch meaningful ownership fields (state, assignee, description hash) to detect real human edits without false alarms."""
        data = self.query(
            """
            query($id: String!) {
              issue(id: $id) {
                updatedAt
                description
                assignee { id }
                state { id name type }
              }
            }
            """,
            {"id": issue_uuid},
        )
        iss = data.get("issue") or {}
        st = iss.get("state") or {}
        return {
            "updated_at": str(iss.get("updatedAt") or ""),
            "state_id": str(st.get("id") or ""),
            "state_name": str(st.get("name") or ""),
            "state_type": str(st.get("type") or ""),
            "assignee_id": str((iss.get("assignee") or {}).get("id") or ""),
            "description_sha": sha256_text(str(iss.get("description") or "")),
        }

    def list_project_issues(self, project_id: str, *, state_type: str | None = None) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        filter_clause = ", filter: { state: { type: { eq: $stateType } } }" if state_type else ""
        var_decl = "$projectId: String!, $after: String" + (", $stateType: String" if state_type else "")
        while True:
            variables: dict[str, Any] = {"projectId": project_id, "after": cursor}
            if state_type:
                variables["stateType"] = state_type
            data = self.query(
                f"""
                query({var_decl}) {{
                  project(id: $projectId) {{
                    id
                    name
                    issues(first: 50, after: $after{filter_clause}) {{
                      nodes {{
                        id
                        identifier
                        title
                        description
                        url
                        priority
                        assignee {{ id name }}
                        state {{ id name type }}
                        labels {{ nodes {{ id name }} pageInfo {{ hasNextPage }} }}
                        parent {{ id identifier }}
                        children(first: 1) {{ nodes {{ id }} }}
                      }}
                      pageInfo {{ hasNextPage endCursor }}
                    }}
                  }}
                }}
                """,
                variables,
            )
            proj = data.get("project")
            if not proj:
                raise RuntimeError(f"Linear project '{project_id}' not found.")
            conn = proj["issues"]
            self._complete_issue_labels(conn["nodes"])
            issues.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                break
            nxt = conn["pageInfo"].get("endCursor")
            if not nxt or nxt in seen_cursors:
                raise RuntimeError(f"Linear pagination stalled on project.issues (cursor={nxt!r})")
            seen_cursors.add(nxt)
            cursor = nxt
        return issues

    def get_issue_inverse_relations(self, issue_id: str) -> list[dict[str, Any]]:
        return self._paginate_entity_field(
            "issue", issue_id, "inverseRelations", "type issue { id identifier title state { name type } }"
        )

    def update_issue_state_by_id(self, issue: dict[str, Any], target_state_id: str, label_hint: str = "") -> dict[str, Any]:
        if (issue.get("state") or {}).get("id") == target_state_id:
            return issue["state"]
        data = self.query(
            """
            mutation($id: String!, $stateId: String!) {
              issueUpdate(id: $id, input: { stateId: $stateId }) {
                success
                issue { id identifier updatedAt state { id name type } }
              }
            }
            """,
            {"id": issue["id"], "stateId": target_state_id},
            retry=False,
        )
        upd = data.get("issueUpdate") or {}
        returned_issue = upd.get("issue") or {}
        returned_state = returned_issue.get("state") or {}
        if not upd.get("success") or returned_state.get("id") != target_state_id:
            raise RuntimeError(
                f"Linear issueUpdate failed verification for {issue['identifier']} -> {label_hint or target_state_id}: {upd}"
            )
        issue["state"] = returned_state
        if returned_issue.get("updatedAt"):
            issue["updatedAt"] = returned_issue["updatedAt"]
        return returned_state

    def update_issue_state(self, issue: dict[str, Any], target_state: str) -> dict[str, Any]:
        states = issue.get("team", {}).get("states", {}).get("nodes", [])
        match = next((s for s in states if s["name"].lower() == target_state.lower()), None)
        if not match:
            avail = [s["name"] for s in states]
            raise RuntimeError(f"Linear state '{target_state}' not found in team states: {avail}")
        return self.update_issue_state_by_id(issue, match["id"], label_hint=target_state)

    def _find_or_create_label(self, issue: dict[str, Any], label_name: str) -> dict[str, Any]:
        team = issue.get("team") or {}
        team_labels = (team.get("labels") or {}).get("nodes", [])
        match = next((l for l in team_labels if l["name"].lower() == label_name.lower()), None)
        if match:
            return match
        workspace = self.query(
            """query($name: String!) {
                issueLabels(filter: { name: { eqIgnoreCase: $name }, team: { null: true } }, first: 1) {
                    nodes { id name }
                }
            }""",
            {"name": label_name},
        )["issueLabels"]["nodes"]
        if workspace:
            return workspace[0]

        try:
            created = self.query(
                """
                mutation($teamId: String!, $name: String!) {
                  issueLabelCreate(input: { teamId: $teamId, name: $name }) {
                    success
                    issueLabel { id name }
                  }
                }
                """,
                {"teamId": team["id"], "name": label_name},
                retry=False,
            )
            res = created.get("issueLabelCreate") or {}
            if res.get("success") and res.get("issueLabel"):
                match = res["issueLabel"]
                team_labels.append(match)
                return match
            raise RuntimeError(f"Failed to create Linear label {label_name!r}")
        except RuntimeError:
            # Handle concurrent creation race from a sibling worker: refresh team and workspace labels once before failing
            fresh_team_labels = self._paginate_entity_field("team", team["id"], "labels", "id name")
            team["labels"] = {"nodes": fresh_team_labels}
            raced = next((l for l in fresh_team_labels if l["name"].lower() == label_name.lower()), None)
            if raced:
                return raced
            raced_ws = self.query(
                """query($name: String!) {
                    issueLabels(filter: { name: { eqIgnoreCase: $name }, team: { null: true } }, first: 1) {
                        nodes { id name }
                    }
                }""",
                {"name": label_name},
            )["issueLabels"]["nodes"]
            if raced_ws:
                return raced_ws[0]
            raise

    def ensure_label_and_add(self, issue: dict[str, Any], label_name: str) -> None:
        fresh_labels = self._paginate_entity_field("issue", issue["id"], "labels", "id name")
        issue["labels"] = {"nodes": fresh_labels}
        if any(l["name"].lower() == label_name.lower() for l in fresh_labels):
            return
        match = self._find_or_create_label(issue, label_name)
        self._change_label(issue["id"], match["id"], add=True)
        fresh_labels.append(match)

    def _change_label(self, issue_id: str, label_id: str, *, add: bool) -> None:
        operation = "issueAddLabel" if add else "issueRemoveLabel"
        data = self.query(
            f"""mutation($id: String!, $labelId: String!) {{
                {operation}(id: $id, labelId: $labelId) {{ success }}
            }}""",
            {"id": issue_id, "labelId": label_id},
            retry=False,
        )
        if not (data.get(operation) or {}).get("success"):
            raise RuntimeError(f"{operation} failed for issue {issue_id}")

    def remove_labels_if_present(self, issue: dict[str, Any], label_names: set[str]) -> None:
        labels = self._paginate_entity_field("issue", issue["id"], "labels", "id name")
        targets = {n.lower() for n in label_names}
        for label in labels:
            if label["name"].lower() in targets:
                self._change_label(issue["id"], label["id"], add=False)
        issue["labels"] = {"nodes": [label for label in labels if label["name"].lower() not in targets]}

    def add_comment(self, issue_id: str, body: str) -> str:
        safe_body = redact_secrets(body)
        data = self.query(
            """
            mutation($issueId: String!, $body: String!) {
              commentCreate(input: { issueId: $issueId, body: $body }) {
                success
                comment { id }
              }
            }
            """,
            {"issueId": issue_id, "body": safe_body},
            retry=False,
        )
        res = data.get("commentCreate") or {}
        if not res.get("success"):
            raise RuntimeError(f"Linear commentCreate returned success=false for issue {issue_id}")
        return str((res.get("comment") or {}).get("id", ""))


def normalize_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def resolve_project(client: LinearClient, repo_root: Path, explicit: str | None = None) -> dict[str, Any]:
    _, projects = client.list_teams_and_projects()

    def _match(candidate: str) -> list[dict[str, Any]]:
        nc = normalize_name(candidate)
        matches = [p for p in projects if p["id"] == candidate or p["name"].lower() == candidate.lower()]
        if not matches and nc:
            matches = [p for p in projects if normalize_name(p["name"]) == nc]
        return matches

    explicit_source: tuple[str, str] | None = None
    if explicit:
        explicit_source = ("--project", explicit.strip())
    else:
        marker = repo_root / ".linear-project"
        if marker.is_file():
            val = marker.read_text(encoding="utf-8").strip()
            if val:
                explicit_source = (".linear-project", val)

    if explicit_source:
        src, val = explicit_source
        matches = _match(val)
        if len(matches) == 1:
            return matches[0]
        avail = ", ".join(p["name"] for p in projects) or "(none)"
        if len(matches) > 1:
            raise RuntimeError(f"Ambiguous Linear project '{val}' from {src}: {[m['name'] for m in matches]}")
        raise RuntimeError(f"Explicit Linear project '{val}' from {src} not found. Available: {avail}")

    candidates: list[str] = []
    remote = run_cmd(git_cmd("remote", "get-url", "origin"), cwd=repo_root, env_role="orchestrator")
    if remote.returncode == 0 and remote.stdout.strip():
        slug = remote.stdout.strip().rstrip("/").rsplit("/", 1)[-1]
        if slug.endswith(".git"):
            slug = slug[:-4]
        candidates.append(slug)
    candidates.append(repo_root.name)

    for cand in candidates:
        matches = _match(cand)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(
                f"Multiple Linear projects match '{cand}': {[m['name'] for m in matches]}. Set .linear-project."
            )

    avail = ", ".join(p["name"] for p in projects) or "(none)"
    raise RuntimeError(
        f"Could not match repo '{repo_root.name}' to a Linear project (tried: {candidates}). "
        f"Available: {avail}. Pass --project <name> or create .linear-project."
    )
