"""Linear client implementation backed by local Codex MCP bridge."""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import threading
import time
from typing import Any

from ai_engineering.linear_dispatcher.contracts import LinearTask


class LinearCodexClient:
    """Production Linear client executing via Codex MCP linear integration."""

    def __init__(
        self,
        port: int = 11434,
        codex_bin: str | None = None,
        timeout_sec: int = 25,
    ) -> None:
        self.port = port
        self.codex_bin = codex_bin or r"C:\Users\Oleg\AppData\Local\OpenAI\Codex\bin\faa963e871dd422c\codex.exe"
        self.timeout_sec = timeout_sec

    def _call_mcp(self, func_name: str, arguments: dict[str, Any]) -> Any:
        result_holder: dict[str, Any] = {}
        server_turn = [0]

        class BridgeHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if self.path == "/api/tags":
                    self.wfile.write(b'{"models":[{"name":"gpt-oss:20b","model":"gpt-oss:20b"}]}')
                elif self.path == "/api/version":
                    self.wfile.write(b'{"version":"0.14.0"}')
                elif "/v1/models" in self.path:
                    self.wfile.write(b'{"object":"list","data":[{"id":"gpt-oss:20b","object":"model","created":1700000000,"owned_by":"ollama"}]}')
                else:
                    self.wfile.write(b'{}')

            def do_POST(self):
                server_turn[0] += 1
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                body_json = json.loads(body.decode("utf-8", errors="ignore"))

                if "input" in body_json:
                    for item in body_json["input"]:
                        if item.get("type") == "function_call_output":
                            out_list = item.get("output", [])
                            full_text = "\n".join([x.get("text", "") for x in out_list if isinstance(x, dict)])
                            lines = full_text.splitlines()
                            json_str = None
                            for i, l in enumerate(lines):
                                l_str = l.strip()
                                if l_str.startswith("{") or l_str.startswith("["):
                                    json_str = "\n".join(lines[i:])
                                    break
                            if json_str:
                                try:
                                    result_holder["data"] = json.loads(json_str)
                                except Exception:
                                    result_holder["raw"] = json_str
                            else:
                                result_holder["raw"] = full_text

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                resp_id = f"resp_{server_turn[0]}"
                if server_turn[0] == 1:
                    fc_item = {
                        "id": "fc_001",
                        "type": "function_call",
                        "name": func_name,
                        "namespace": "mcp__codex_apps__linear",
                        "arguments": json.dumps(arguments),
                        "call_id": "call_001",
                    }
                    events = [
                        {"type": "response.created", "response": {"id": resp_id, "status": "in_progress"}},
                        {"type": "response.output_item.added", "item": fc_item},
                        {"type": "response.output_item.done", "item": fc_item},
                        {"type": "response.completed", "response": {"id": resp_id, "status": "completed"}},
                    ]
                else:
                    msg_item = {
                        "id": "msg_done",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "DONE"}],
                    }
                    events = [
                        {"type": "response.created", "response": {"id": resp_id, "status": "in_progress"}},
                        {"type": "response.output_item.added", "item": msg_item},
                        {"type": "response.output_item.done", "item": msg_item},
                        {"type": "response.completed", "response": {"id": resp_id, "status": "completed"}},
                    ]

                for ev in events:
                    msg = f"data: {json.dumps(ev)}\n\n".encode("utf-8")
                    self.wfile.write(msg)
                    self.wfile.flush()

                time.sleep(0.3)

            def log_message(self, format, *args):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", self.port), BridgeHandler) as httpd:
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                cmd = [
                    self.codex_bin,
                    "exec",
                    "--oss",
                    "--local-provider",
                    "ollama",
                    "--dangerously-bypass-approvals-and-sandbox",
                    f"exec {func_name}",
                ]
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout, stderr = proc.communicate(input=b"", timeout=self.timeout_sec)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
            finally:
                httpd.shutdown()

        return result_holder.get("data") or result_holder.get("raw")

    def _parse_task(self, raw: dict[str, Any]) -> LinearTask:
        # Handles raw issue object from Linear
        p_val = raw.get("priority")
        if isinstance(p_val, dict):
            p_num = int(p_val.get("value", 3))
        elif isinstance(p_val, (int, float)):
            p_num = int(p_val)
        else:
            p_num = 3

        labels_raw = raw.get("labels", [])
        label_names = tuple(
            l.get("name", "") if isinstance(l, dict) else str(l)
            for l in labels_raw
        )

        return LinearTask(
            id=str(raw.get("id", "")),
            uuid=str(raw.get("uuid", "")),
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "") or ""),
            assignee=raw.get("assignee") if isinstance(raw.get("assignee"), str) else (raw.get("assignee") or {}).get("name"),
            priority=p_num,
            state=str(raw.get("status") or raw.get("state") or ""),
            labels=label_names,
            url=str(raw.get("url", "")),
            created_at=str(raw.get("createdAt", "")),
            updated_at=str(raw.get("updatedAt", "")),
        )

    def add_comment(self, issue_id: str, body: str) -> bool:
        res = self._call_mcp("_save_comment", {"issueId": issue_id, "body": body})
        if isinstance(res, dict) and (res.get("id") or res.get("body") or not res.get("error_code")):
            return True
        return False

    def update_issue(self, issue_id: str, fields: dict[str, Any]) -> bool:
        args = {"id": issue_id, **fields}
        res = self._call_mcp("_save_issue", args)
        if isinstance(res, dict) and (res.get("id") or not res.get("error_code")):
            return True
        return False

    def get_issue(self, issue_id: str) -> LinearTask | None:
        res = self._call_mcp("_get_issue", {"id": issue_id})
        if isinstance(res, dict) and res.get("id"):
            return self._parse_task(res)
        return None

    def get_issue_comments(self, issue_id: str) -> list[str]:
        res = self._call_mcp("_list_comments", {"issueId": issue_id, "limit": 250})
        if isinstance(res, dict):
            comments = res.get("comments", [])
            return [str(c.get("body", "")) for c in comments if isinstance(c, dict)]
        return []

    def list_issues(self, team: str = "Hermes", limit: int = 50) -> list[LinearTask]:
        res = self._call_mcp("_list_issues", {"team": team, "limit": limit})
        tasks: list[LinearTask] = []
        if isinstance(res, dict):
            issues = res.get("issues", [])
            for iss in issues:
                if isinstance(iss, dict) and iss.get("id"):
                    tasks.append(self._parse_task(iss))
        return tasks
