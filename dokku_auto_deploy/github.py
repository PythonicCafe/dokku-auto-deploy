"""GitHub REST API (github.com and GitHub Enterprise Server): the handful of calls the deploy cycle needs."""

import urllib.parse
from typing import Any

from dokku_auto_deploy.forge import Change, CIStatus, Forge
from dokku_auto_deploy.repository import Repository


class GitHub(Forge):
    name = "GitHub"

    def __init__(self, repository: Repository, token: str) -> None:
        super().__init__(repository.api_url)
        self.repository = repository
        self.token = token
        self.base = f"/repos/{repository.path}"

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def branch_head(self, branch: str) -> str:
        path = f"{self.base}/branches/{urllib.parse.quote(branch, safe='')}"
        return str(self.request("GET", path)["commit"]["sha"])

    def ci_status(self, sha: str, branch: str, workflow: str) -> CIStatus:
        """Runs triggered by pushes of `sha` to `branch`; the API can't filter by workflow file, so it's done here."""
        params = {"head_sha": sha, "branch": branch, "event": "push", "per_page": 100}
        runs: list[dict[str, Any]] = self.request("GET", f"{self.base}/actions/runs", params)["workflow_runs"]
        matching = [run for run in runs if run.get("path") == workflow]
        if not matching:
            return "missing"
        latest = max(matching, key=lambda run: int(run["id"]))
        if latest["status"] != "completed":
            return "pending"
        return "success" if latest["conclusion"] == "success" else "failure"

    def commits_between(self, base: str, head: str) -> set[str]:
        comparison = self.request("GET", f"{self.base}/compare/{base}...{head}")
        return {commit["sha"] for commit in comparison["commits"]}

    def merged_changes(self, branch: str) -> list[Change]:
        """Most recently updated closed PRs into `branch` (one page of 100 is enough for a polling interval).

        `merge_commit_sha` is the merge commit, the squashed commit or the last rebased commit, so it identifies the
        PR in the branch history whatever the merge method.
        """
        params = {"state": "closed", "base": branch, "sort": "updated", "direction": "desc", "per_page": 100}
        pulls: list[dict[str, Any]] = self.request("GET", f"{self.base}/pulls", params)
        return [
            Change(
                number=int(pull["number"]),
                reference=f"#{pull['number']}",
                title=pull.get("title") or "",
                url=pull["html_url"],
                shas=frozenset({pull["merge_commit_sha"]}),
            )
            for pull in pulls
            if pull.get("merged_at") and pull.get("merge_commit_sha")
        ]

    def comment(self, change: Change, body: str) -> None:
        self.request("POST", f"{self.base}/issues/{change.number}/comments", payload={"body": body})

    def commit_url(self, sha: str) -> str:
        return f"{self.repository.url}/commit/{sha}"
