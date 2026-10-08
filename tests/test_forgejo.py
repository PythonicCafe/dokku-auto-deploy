import pytest

from dokku_auto_deploy.forge import ForgeError
from dokku_auto_deploy.forgejo import Forgejo
from dokku_auto_deploy.poll import load_state, poll, save_state
from dokku_auto_deploy.repository import Repository

API = "/api/v1/repos/Org/proj"
WORKFLOW = ".forgejo/workflows/ci.yml"


@pytest.fixture
def forgejo(fake_api):
    return Forgejo(Repository(f"{fake_api.url}/Org/proj", "forgejo"), "TOKEN")


def run(status="success", run_id=1, workflow_id="ci.yml", prettyref="develop"):
    return {"id": run_id, "status": status, "workflow_id": workflow_id, "prettyref": prettyref, "event": "push"}


def pull(number, merge_commit_sha, merged=True, base="develop"):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://codeberg.org/Org/proj/pulls/{number}",
        "merged": merged,
        "merge_commit_sha": merge_commit_sha,
        "base": {"ref": base},
    }


def test_branch_head_with_token_header(forgejo, fake_api):
    fake_api.routes[f"GET {API}/branches/feature/x"] = (200, {"commit": {"id": "abc"}})
    assert forgejo.branch_head("feature/x") == "abc"
    assert fake_api.headers[0]["Authorization"] == "token TOKEN"


class TestCIStatus:
    @pytest.mark.parametrize(
        "runs, expected",
        [
            pytest.param([], "missing", id="no-run"),
            pytest.param([run(workflow_id="other.yml")], "missing", id="other-workflow"),
            pytest.param([run(prettyref="main")], "missing", id="other-branch"),
            pytest.param([run(status="waiting")], "pending", id="waiting"),
            pytest.param([run(status="running")], "pending", id="running"),
            pytest.param([run()], "success", id="green"),
            pytest.param([run(status="failure")], "failure", id="red"),
            pytest.param([run(status="cancelled")], "failure", id="cancelled"),
            pytest.param([run(status="failure", run_id=1), run(run_id=2)], "success", id="rerun-green-wins"),
        ],
    )
    def test_latest_run_of_the_workflow_file_on_the_branch(self, forgejo, fake_api, runs, expected):
        fake_api.routes[f"GET {API}/actions/runs"] = (200, {"workflow_runs": runs, "total_count": len(runs)})
        assert forgejo.ci_status("abc", "develop", WORKFLOW) == expected
        ((_, _, query, _),) = fake_api.requests
        assert (query["head_sha"], query["event"]) == ("abc", "push")
        assert (query["ref"], query["workflow_id"]) == ("refs/heads/develop", "ci.yml")

    def test_github_workflows_directory_works_too(self, forgejo, fake_api):
        fake_api.routes[f"GET {API}/actions/runs"] = (200, {"workflow_runs": [run()], "total_count": 1})
        assert forgejo.ci_status("abc", "develop", ".github/workflows/ci.yml") == "success"


def test_merged_changes_filter_merged_into_the_branch(forgejo, fake_api):
    fake_api.routes[f"GET {API}/pulls"] = (
        200,
        [pull(1, "m1"), pull(2, "m2", merged=False), pull(3, "m3", base="main"), None],
    )
    (change,) = forgejo.merged_changes("develop").changes
    assert (change.number, change.reference, change.title, change.shas) == (1, "#1", "PR 1", frozenset({"m1"}))
    assert fake_api.requests[0][2]["state"] == "closed"


def test_comment_on_the_pull_request(forgejo, fake_api):
    fake_api.routes[f"GET {API}/pulls"] = (200, [pull(1, "m1")])
    fake_api.routes[f"POST {API}/issues/1/comments"] = (201, {})
    forgejo.comment(forgejo.merged_changes("develop").changes[0], "hello")
    assert fake_api.posts() == [(f"{API}/issues/1/comments", {"body": "hello"})]


def test_http_error_message(forgejo, fake_api):
    fake_api.routes[f"GET {API}/branches/main"] = (404, {"message": "GetBranch", "url": "https://codeberg.org/api"})
    with pytest.raises(ForgeError, match="Forgejo API GET /repos/Org/proj/branches/main: HTTP 404 GetBranch"):
        forgejo.branch_head("main")


def test_urls(forgejo, fake_api):
    assert forgejo.commit_url("abc") == f"{fake_api.url}/Org/proj/commit/abc"
    assert Repository("https://codeberg.org/Org/proj", "forgejo").api_url == "https://codeberg.org/api/v1"


def test_deploy_cycle(dokku_env, fake_api, fake_dokku):
    dokku_env.git_auth("127.0.0.1", "FJ")
    dokku_env.configure(
        "proj-stg",
        repository=f"{fake_api.url}/Org/proj",
        forge="forgejo",
        branch="develop",
        workflow=WORKFLOW,
        notify="comment",
    )
    save_state(dokku_env.properties, "proj-stg", {"sha": "c1", "status": "deployed", "deployed_sha": "c1"})
    fake_api.routes[f"GET {API}/branches/develop"] = (200, {"commit": {"id": "c3"}})
    fake_api.routes[f"GET {API}/actions/runs"] = (200, {"workflow_runs": [run()], "total_count": 1})
    fake_api.routes[f"GET {API}/compare/c1...c3"] = (200, {"commits": [{"sha": "c2"}, {"sha": "c3"}]})
    fake_api.routes[f"GET {API}/pulls"] = (200, [pull(8, "c3"), pull(7, "c1")])
    fake_api.routes[f"POST {API}/issues/8/comments"] = (201, {})
    fake_dokku.set()
    assert poll(dokku_env.properties).errors == 0
    assert fake_dokku.syncs == [f"git:sync --build proj-stg {fake_api.url}/Org/proj.git c3"]
    assert [path for path, _ in fake_api.posts()] == [f"{API}/issues/8/comments"]
    assert load_state(dokku_env.properties, "proj-stg")["deployed_sha"] == "c3"
