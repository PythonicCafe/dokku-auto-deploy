import pytest

from dokku_auto_deploy.forge import ForgeError
from dokku_auto_deploy.gitlab import GitLab
from dokku_auto_deploy.poll import load_state, poll, save_state
from dokku_auto_deploy.repository import Repository

API = "/api/v4/projects/group%2Fsub%2Fproj"


@pytest.fixture
def gitlab(fake_api):
    return GitLab(Repository(f"{fake_api.url}/group/sub/proj", "gitlab"), "TOKEN")


def pipeline(status, pipeline_id=1):
    return {"id": pipeline_id, "status": status, "sha": "abc", "ref": "develop", "source": "push"}


def merge_request(iid, merge_commit_sha=None, squash_commit_sha=None, sha=None, title="Adds X"):
    return {
        "iid": iid,
        "title": title,
        "web_url": f"https://gitlab.com/group/sub/proj/-/merge_requests/{iid}",
        "merge_commit_sha": merge_commit_sha,
        "squash_commit_sha": squash_commit_sha,
        "sha": sha,
    }


def test_nested_project_path_is_url_encoded_and_token_goes_in_a_header(gitlab, fake_api):
    fake_api.routes[f"GET {API}/repository/branches/feature%2Fx"] = (200, {"commit": {"id": "abc"}})
    assert gitlab.branch_head("feature/x") == "abc"
    assert fake_api.headers[0]["Private-Token"] == "TOKEN"


class TestCIStatus:
    @pytest.mark.parametrize(
        "pipelines, expected",
        [
            pytest.param([], "missing", id="no-pipeline"),
            pytest.param([pipeline("running")], "pending", id="running"),
            pytest.param([pipeline("manual")], "pending", id="waiting-for-manual-job"),
            pytest.param([pipeline("success")], "success", id="green"),
            pytest.param([pipeline("failed")], "failure", id="red"),
            pytest.param([pipeline("canceled")], "failure", id="canceled"),
            pytest.param([pipeline("skipped")], "failure", id="skipped"),
            pytest.param([pipeline("success", 2), pipeline("failed", 1)], "success", id="retry-green-wins"),
            pytest.param([pipeline("failed", 1), pipeline("pending", 2)], "pending", id="retry-running"),
        ],
    )
    def test_latest_push_pipeline_counts(self, gitlab, fake_api, pipelines, expected):
        fake_api.routes[f"GET {API}/pipelines"] = (200, pipelines)
        assert gitlab.ci_status("abc", "develop", ".gitlab-ci.yml") == expected
        ((_, _, query, _),) = fake_api.requests
        assert (query["sha"], query["ref"], query["source"]) == ("abc", "develop", "push")


def test_merged_changes_are_identified_by_merge_squash_or_fast_forward_head_commit(gitlab, fake_api):
    """Regression: the head commit counted even with a merge commit, so an MR whose branch another MR had built on
    was notified for that other MR's deploy."""
    fake_api.routes[f"GET {API}/merge_requests"] = (
        200,
        [
            merge_request(3, merge_commit_sha="m3", sha="h3"),
            merge_request(4, squash_commit_sha="s4", sha="h4"),
            merge_request(5, sha="h5"),
        ],
    )
    first, second, third = gitlab.merged_changes("develop").changes
    assert (first.number, first.reference, first.shas) == (3, "!3", frozenset({"m3"}))
    assert second.shas == frozenset({"s4"})
    assert third.shas == frozenset({"h5"})
    query = fake_api.requests[0][2]
    assert (query["state"], query["target_branch"]) == ("merged", "develop")


def test_commits_between_uses_compare(gitlab, fake_api):
    fake_api.routes[f"GET {API}/repository/compare"] = (200, {"commits": [{"id": "c2"}, {"id": "c3"}]})
    assert gitlab.commits_between("c1", "c3") == {"c2", "c3"}
    assert fake_api.requests[0][2] == {"from": "c1", "to": "c3"}


def test_comment_is_a_merge_request_note(gitlab, fake_api):
    fake_api.routes[f"GET {API}/merge_requests"] = (200, [merge_request(3, merge_commit_sha="m3")])
    fake_api.routes[f"POST {API}/merge_requests/3/notes"] = (201, {})
    gitlab.comment(gitlab.merged_changes("develop").changes[0], "hello")
    assert fake_api.posts() == [(f"{API}/merge_requests/3/notes", {"body": "hello"})]


def test_http_error_message(gitlab, fake_api):
    fake_api.routes[f"GET {API}/repository/branches/main"] = (404, {"message": "404 Project Not Found"})
    with pytest.raises(ForgeError, match="GitLab API GET .*: HTTP 404 404 Project Not Found"):
        gitlab.branch_head("main")


def test_urls(gitlab, fake_api):
    assert gitlab.commit_url("abc") == f"{fake_api.url}/group/sub/proj/-/commit/abc"
    assert Repository("https://gitlab.com/group/proj", "gitlab").api_url == "https://gitlab.com/api/v4"


def test_deploy_cycle(dokku_env, fake_api, fake_dokku):
    dokku_env.git_auth("127.0.0.1", "GL")
    dokku_env.configure(
        "proj-stg",
        repository=f"{fake_api.url}/group/sub/proj",
        forge="gitlab",
        branch="develop",
        workflow=".gitlab-ci.yml",
        notify="comment",
    )
    save_state(dokku_env.properties, "proj-stg", {"sha": "c1", "status": "deployed", "deployed_sha": "c1"})
    fake_api.routes[f"GET {API}/repository/branches/develop"] = (200, {"commit": {"id": "c3"}})
    fake_api.routes[f"GET {API}/pipelines"] = (200, [pipeline("success")])
    fake_api.routes[f"GET {API}/repository/compare"] = (200, {"commits": [{"id": "c2"}, {"id": "c3"}]})
    fake_api.routes[f"GET {API}/merge_requests"] = (
        200,
        [merge_request(5, squash_commit_sha="c3", sha="h5"), merge_request(4, merge_commit_sha="c1")],
    )
    fake_api.routes[f"POST {API}/merge_requests/5/notes"] = (201, {})
    fake_dokku.set()
    assert poll(dokku_env.properties).errors == 0
    assert fake_dokku.syncs == [f"git:sync --build proj-stg {fake_api.url}/group/sub/proj.git c3"]
    ((path, body),) = fake_api.posts()
    assert path == f"{API}/merge_requests/5/notes"
    assert f"({fake_api.url}/group/sub/proj/-/commit/c3)" in body["body"]
    assert load_state(dokku_env.properties, "proj-stg")["deployed_sha"] == "c3"


def test_redirect_to_another_origin_drops_the_private_token(gitlab, fake_api, other_api):
    fake_api.redirects[f"GET {API}/repository/branches/main"] = f"{other_api.url}/elsewhere"
    other_api.routes["GET /elsewhere"] = (200, {"commit": {"id": "abc"}})
    assert gitlab.branch_head("main") == "abc"
    assert "Private-Token" not in other_api.headers[0]
