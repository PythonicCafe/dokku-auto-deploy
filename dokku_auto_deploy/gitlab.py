"""GitLab REST API (gitlab.com and self-managed): the handful of calls the deploy cycle needs."""

import urllib.parse
from typing import Any

from dokku_auto_deploy.forge import Change, CIStatus, Forge, MergedChanges
from dokku_auto_deploy.repository import Repository

PAGE_SIZE = 100
PENDING_STATUSES = frozenset(
    {
        "created",
        "waiting_for_resource",
        "preparing",
        "waiting_for_callback",
        "pending",
        "running",
        "canceling",
        "manual",
        "scheduled",
    }  # fmt: skip
)
FAILED_STATUSES = frozenset({"failed", "canceled", "skipped"})


def _merged_shas(request: dict[str, Any]) -> frozenset[str]:
    shas = {sha for sha in (request.get("merge_commit_sha"), request.get("squash_commit_sha")) if sha}
    if not shas and request.get("sha"):  # Fast-forward merge
        shas.add(request["sha"])
    return frozenset(shas)


class GitLab(Forge):
    name = "GitLab"

    def __init__(self, repository: Repository, token: str) -> None:
        super().__init__(repository.api_url)
        self.repository = repository
        self.token = token
        self.base = f"/projects/{urllib.parse.quote(repository.path, safe='')}"

    def headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def branch_head(self, branch: str) -> str:
        path = f"{self.base}/repository/branches/{urllib.parse.quote(branch, safe='')}"
        return str(self.request("GET", path)["commit"]["id"])

    def ci_status(self, sha: str, branch: str, workflow: str) -> CIStatus:
        """The latest pipeline of `sha` pushed to `branch`. GitLab runs one pipeline per push, defined by the project's
        CI configuration, so `workflow` isn't used to filter. A pipeline waiting for a manual job counts as pending."""
        params = {"sha": sha, "ref": branch, "source": "push", "order_by": "id", "sort": "desc", "per_page": 20}
        pipelines: list[dict[str, Any]] = self.request("GET", f"{self.base}/pipelines", params)
        if not pipelines:
            return "missing"
        status = max(pipelines, key=lambda pipeline: int(pipeline["id"]))["status"]
        if status == "success":
            return "success"
        return "failure" if status in FAILED_STATUSES else "pending"

    def commits_between(self, base: str, head: str) -> set[str]:
        comparison = self.request("GET", f"{self.base}/repository/compare", {"from": base, "to": head})
        return {commit["id"] for commit in comparison["commits"]}

    def merged_changes(self, branch: str) -> MergedChanges:
        """Most recently updated merge requests merged into `branch` (100 are enough for a polling interval).

        The merge request is in the branch history through its merge commit or its squash commit. Only a fast-forward
        merge has neither, and then its own head commit is the one on the branch. The head commit isn't used otherwise:
        it can reach the branch through another merge request (a branch built on top of this one's), which would
        attribute that deploy to this merge request.
        """
        params = {
            "state": "merged",
            "target_branch": branch,
            "order_by": "updated_at",
            "sort": "desc",
            "per_page": PAGE_SIZE,
        }
        requests: list[dict[str, Any]] = self.request("GET", f"{self.base}/merge_requests", params)
        changes = [
            Change(
                number=int(request["iid"]),
                reference=f"!{request['iid']}",
                title=request.get("title") or "",
                url=request["web_url"],
                shas=_merged_shas(request),
            )
            for request in requests
        ]
        return MergedChanges(changes, page_full=len(requests) >= PAGE_SIZE)

    def comment(self, change: Change, body: str) -> None:
        self.request("POST", f"{self.base}/merge_requests/{change.number}/notes", payload={"body": body})

    def commit_url(self, sha: str) -> str:
        return f"{self.repository.url}/-/commit/{sha}"
