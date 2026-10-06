"""Production Linear client using standard API and repository credentials."""

import os
import json
import urllib.request
import urllib.error
from typing import Any

from ai_engineering.linear_dispatcher.contracts import LinearTask

class LinearProductionClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY")
        if not self.api_key:
            raise ValueError("LINEAR_API_KEY environment variable is required")
        self.base_url = "https://api.linear.app/graphql"

    def _graphql_request(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps({"query": query, "variables": variables or {}}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.URLError as e:
            # We don't expose secrets in logs, just raise generic or safe error
            raise RuntimeError(f"Linear API request failed: {e}")

    def _parse_task(self, node: dict[str, Any]) -> LinearTask:
        labels_node = node.get("labels", {}).get("nodes", [])
        label_names = tuple(l.get("name", "") for l in labels_node)
        assignee_node = node.get("assignee")
        assignee_name = assignee_node.get("name") if assignee_node else None

        return LinearTask(
            id=node.get("identifier", ""),
            uuid=node.get("id", ""),
            title=node.get("title", ""),
            description=node.get("description", ""),
            assignee=assignee_name,
            priority=node.get("priority", 0),
            state=node.get("state", {}).get("name", ""),
            labels=label_names,
            url=node.get("url", ""),
            created_at=node.get("createdAt", ""),
            updated_at=node.get("updatedAt", ""),
        )

    def list_issues(self, team: str = "Hermes", limit: int = 50) -> list[LinearTask]:
        query = """
        query($limit: Int) {
          issues(first: $limit, filter: { team: { name: { eq: "Hermes" } } }) {
            nodes {
              id
              identifier
              title
              description
              priority
              url
              createdAt
              updatedAt
              state { name }
              assignee { name }
              labels { nodes { name } }
            }
          }
        }
        """
        res = self._graphql_request(query, {"limit": limit})
        nodes = res.get("data", {}).get("issues", {}).get("nodes", [])
        return [self._parse_task(node) for node in nodes if node.get("identifier")]

    def get_issue(self, issue_id: str) -> LinearTask | None:
        query = """
        query($id: String!) {
          issue(id: $id) {
            id
            identifier
            title
            description
            priority
            url
            createdAt
            updatedAt
            state { name }
            assignee { name }
            labels { nodes { name } }
          }
        }
        """
        res = self._graphql_request(query, {"id": issue_id})
        node = res.get("data", {}).get("issue")
        if node:
            return self._parse_task(node)
        return None

    def get_issue_comments(self, issue_id: str) -> list[str]:
        query = """
        query($id: String!) {
          issue(id: $id) {
            comments {
              nodes {
                body
              }
            }
          }
        }
        """
        res = self._graphql_request(query, {"id": issue_id})
        nodes = res.get("data", {}).get("issue", {}).get("comments", {}).get("nodes", [])
        return [n.get("body", "") for n in nodes]

    def add_comment(self, issue_id: str, body: str) -> bool:
        query = """
        mutation($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) {
            success
          }
        }
        """
        issue = self.get_issue(issue_id)
        if not issue:
            return False
            
        res = self._graphql_request(query, {"issueId": issue.uuid, "body": body})
        return res.get("data", {}).get("commentCreate", {}).get("success", False)

    def update_issue(self, issue_id: str, fields: dict[str, Any]) -> bool:
        issue = self.get_issue(issue_id)
        if not issue:
            return False

        payload_fields = dict(fields)
        if "state" in payload_fields and "stateId" not in payload_fields:
            state_val = payload_fields.pop("state")
            # If state name provided, resolve to workflow state id scoped to team
            try:
                states_query = """
                query {
                  workflowStates {
                    nodes {
                      id
                      name
                      type
                      team {
                        id
                        name
                        key
                      }
                    }
                  }
                }
                """
                s_res = self._graphql_request(states_query)
                nodes = (
                    s_res.get("data", {})
                    .get("workflowStates", {})
                    .get("nodes", [])
                )
                match = next(
                    (
                        n["id"]
                        for n in nodes
                        if n.get("name", "").lower() == str(state_val).lower()
                        or (
                            str(state_val).lower() in ("done", "completed")
                            and n.get("type", "").lower() in ("completed", "done")
                        )
                    ),
                    None,
                )
                if match:
                    payload_fields["stateId"] = match
                else:
                    payload_fields["stateId"] = str(state_val)
            except Exception:
                payload_fields["stateId"] = str(state_val)

        query = """
        mutation($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
          }
        }
        """
        res = self._graphql_request(query, {"id": issue.uuid, "input": payload_fields})
        if "errors" in res:
            return False
        return res.get("data", {}).get("issueUpdate", {}).get("success", False)
