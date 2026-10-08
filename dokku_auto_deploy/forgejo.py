"""Forgejo REST API (Codeberg and self-hosted, 12+ for Actions runs): the handful of calls the deploy cycle needs."""

import posixpath
import urllib.parse
from typing import Any

from dokku_auto_deploy.forge import Change, CIStatus, Forge, MergedChanges
from dokku_auto_deploy.repository import Repository

FAILED_STATUSES = frozenset({"failure", "cancelled", "skipped"})
PAGE_SIZE = 50  # Forgejo's default maximum page size


class Forgejo(Forge):
    name = "Forgejo"

    def __init__(self, repository: Repository, token: str) -> None:
        super().__init__(repository.api_url)
        self.repository = repository
        self.token = token
        self.base = f"/repos/{repository.path}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    def branch_head(self, branch: str) -> str:
        path = f"{self.base}/branches/{urllib.parse.quote(branch, safe='/')}"
        return str(self.request("GET", path)["commit"]["id"])

    def ci_status(self, sha: str, branch: str, workflow: str) -> CIStatus:
        """Actions runs of `sha` triggered by a push to `branch`, of the workflow file named like `workflow`.

        Forgejo identifies a workflow by its file name (`ci.yml`), whether it lives in `.forgejo/workflows/` or
        `.github/workflows/`. The `ref` and `workflow_id` filters only exist since Forgejo 15 (12 to 14 ignore them
        and answer every run of the commit), so the runs are also filtered here.
        """
        name = posixpath.basename(workflow)
        params = {"head_sha": sha, "event": "push", "ref": f"refs/heads/{branch}", "workflow_id": name}
        params["limit"] = str(PAGE_SIZE)
        runs: list[dict[str, Any]] = self.request("GET", f"{self.base}/actions/runs", params)["workflow_runs"] or []
        matching = [run for run in runs if run.get("workflow_id") == name and run.get("prettyref") == branch]
        if not matching:
            return "missing"
        status = max(matching, key=lambda run: int(run["id"]))["status"]
        if status == "success":
            return "success"
        return "failure" if status in FAILED_STATUSES else "pending"

    def commits_between(self, base: str, head: str) -> set[str]:
        comparison = self.request("GET", f"{self.base}/compare/{base}...{head}")
        return {commit["sha"] for commit in comparison["commits"] or []}

    def merged_changes(self, branch: str) -> MergedChanges:
        """Most recently updated closed pull requests merged into `branch` (the API can't filter by base branch)."""
        params = {"state": "closed", "sort": "recentupdate", "limit": PAGE_SIZE}
        pulls: list[dict[str, Any]] = self.request("GET", f"{self.base}/pulls", params) or []
        changes = [
            Change(
                number=int(pull["number"]),
                reference=f"#{pull['number']}",
                title=pull.get("title") or "",
                url=pull["html_url"],
                shas=frozenset({pull["merge_commit_sha"]}),
            )
            for pull in pulls
            if pull  # A pull request the server failed to load comes as null
            and pull.get("merged")
            and pull.get("merge_commit_sha")
            and (pull.get("base") or {}).get("ref") == branch
        ]
        return MergedChanges(changes, page_full=len(pulls) >= PAGE_SIZE)

    def comment(self, change: Change, body: str) -> None:
        self.request("POST", f"{self.base}/issues/{change.number}/comments", payload={"body": body})

    def commit_url(self, sha: str) -> str:
        return f"{self.repository.url}/commit/{sha}"
