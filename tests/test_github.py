import pytest

from dokku_auto_deploy.forge import ForgeError
from dokku_auto_deploy.github import GitHub
from dokku_auto_deploy.repository import Repository

WORKFLOW = ".github/workflows/ci.yml"


def run(conclusion="success", status="completed", path=WORKFLOW, run_id=1):
    return {"id": run_id, "path": path, "status": status, "conclusion": conclusion}


@pytest.fixture
def github(fake_api):
    return GitHub(Repository(f"{fake_api.url}/Org/proj", "github"), "TOKEN")


class TestCIStatus:
    @pytest.mark.parametrize(
        "runs, expected",
        [
            pytest.param([], "missing", id="not-started"),
            pytest.param([run(path=".github/workflows/other.yml")], "missing", id="other-workflow"),
            pytest.param([run(status="in_progress", conclusion=None)], "pending", id="running"),
            pytest.param([run()], "success", id="green"),
            pytest.param([run(conclusion="failure")], "failure", id="red"),
            pytest.param([run(conclusion="cancelled")], "failure", id="cancelled"),
            pytest.param([run(conclusion="failure", run_id=1), run(run_id=2)], "success", id="rerun-green-wins"),
            pytest.param(
                [run(run_id=1), run(status="queued", conclusion=None, run_id=2)], "pending", id="rerun-running"
            ),
        ],
    )
    def test_latest_run_of_the_workflow_counts(self, github, fake_api, runs, expected):
        fake_api.routes["GET /api/v3/repos/Org/proj/actions/runs"] = (200, {"workflow_runs": runs})
        assert github.ci_status("abc", "develop", WORKFLOW) == expected
        ((_, _, query, _),) = fake_api.requests
        assert (query["head_sha"], query["branch"], query["event"]) == ("abc", "develop", "push")


def test_merged_changes_skip_closed_without_merge(github, fake_api):
    pulls = [
        {
            "number": 7,
            "title": "Adds X",
            "html_url": "https://github.com/Org/proj/pull/7",
            "merged_at": "2026-09-01",
            "merge_commit_sha": "m7",
        },
        {
            "number": 8,
            "title": "Closed",
            "html_url": "https://github.com/Org/proj/pull/8",
            "merged_at": None,
            "merge_commit_sha": "m8",
        },
    ]
    fake_api.routes["GET /api/v3/repos/Org/proj/pulls"] = (200, pulls)
    (change,) = github.merged_changes("develop")
    assert (change.number, change.reference, change.title, change.shas) == (7, "#7", "Adds X", frozenset({"m7"}))
    assert fake_api.requests[0][2]["base"] == "develop"


def test_comment_posts_on_the_pull_request(github, fake_api):
    fake_api.routes["POST /api/v3/repos/Org/proj/issues/7/comments"] = (201, {})
    fake_api.routes["GET /api/v3/repos/Org/proj/pulls"] = (
        200,
        [{"number": 7, "title": "", "html_url": "u", "merged_at": "x", "merge_commit_sha": "m7"}],
    )
    github.comment(github.merged_changes("develop")[0], "hello")
    assert fake_api.posts() == [("/api/v3/repos/Org/proj/issues/7/comments", {"body": "hello"})]


def test_http_error_shows_the_api_message_but_not_the_token(github, fake_api):
    fake_api.routes["GET /api/v3/repos/Org/proj/branches/main"] = (401, {"message": "Bad credentials"})
    with pytest.raises(
        ForgeError, match="GitHub API GET /repos/Org/proj/branches/main: HTTP 401 Bad credentials"
    ) as exc:
        github.branch_head("main")
    assert "TOKEN" not in str(exc.value)


def test_commit_url(github, fake_api):
    assert github.commit_url("abc") == f"{fake_api.url}/Org/proj/commit/abc"


def test_github_com_uses_api_github_com():
    assert GitHub(Repository("https://github.com/Org/proj", "github"), "T").api == "https://api.github.com"
