"""Minimal GitHub REST client (stdlib only): the handful of calls the deploy cycle needs."""

import json
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_API = "https://api.github.com"
HTTP_TIMEOUT = 30


class GitHub:
    def __init__(self, token: str, api: str = DEFAULT_API) -> None:
        self.token = token
        self.api = api.rstrip("/")

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, payload: Any = None) -> Any:
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.load(response)

    def branch_head(self, repository: str, branch: str) -> str:
        path = f"/repos/{repository}/branches/{urllib.parse.quote(branch, safe='')}"
        return str(self._request("GET", path)["commit"]["sha"])

    def push_runs(self, repository: str, branch: str, sha: str) -> list[dict[str, Any]]:
        """Workflow runs triggered by pushes of `sha` to `branch` (all workflows; the caller filters by file)."""
        params = {"head_sha": sha, "branch": branch, "event": "push", "per_page": 100}
        runs: list[dict[str, Any]] = self._request("GET", f"/repos/{repository}/actions/runs", params)["workflow_runs"]
        return runs

    def commits_between(self, repository: str, base: str, head: str) -> set[str]:
        comparison = self._request("GET", f"/repos/{repository}/compare/{base}...{head}")
        return {commit["sha"] for commit in comparison["commits"]}

    def closed_pulls(self, repository: str, base_branch: str) -> list[dict[str, Any]]:
        """Most recently updated closed PRs into `base_branch` (one page of 100 is enough for a polling interval)."""
        params = {"state": "closed", "base": base_branch, "sort": "updated", "direction": "desc", "per_page": 100}
        pulls: list[dict[str, Any]] = self._request("GET", f"/repos/{repository}/pulls", params)
        return pulls

    def comment(self, repository: str, number: int, body: str) -> None:
        self._request("POST", f"/repos/{repository}/issues/{number}/comments", payload={"body": body})
