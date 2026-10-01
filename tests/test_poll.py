import pytest

from dokku_auto_deploy.forge import Change
from dokku_auto_deploy.poll import decide, load_state, poll, save_state, select_merged
from dokku_auto_deploy.properties import GLOBAL

WORKFLOW = ".github/workflows/ci.yml"


def run(conclusion="success", status="completed", path=WORKFLOW, run_id=1):
    return {"id": run_id, "path": path, "status": status, "conclusion": conclusion}


class TestDecide:
    @pytest.mark.parametrize(
        "head, state, ci, expected",
        [
            pytest.param("abc", {"sha": "abc", "status": "deployed"}, "success", "skip", id="already-deployed"),
            pytest.param("abc", {"sha": "abc", "status": "deploy_failed"}, "success", "skip", id="failed-not-retried"),
            pytest.param("new", {"sha": "old", "status": "deployed"}, "success", "deploy", id="new-green"),
            pytest.param("new", None, "success", "deploy", id="first-deploy"),
            pytest.param("new", None, "missing", "wait", id="ci-not-started"),
            pytest.param("new", None, "pending", "wait", id="ci-running"),
            pytest.param("new", None, "failure", "ci_failed", id="ci-failed"),
        ],
    )
    def test_decide(self, head, state, ci, expected):
        assert decide(head, state, ci) == expected

    @pytest.mark.parametrize(
        "state, expected",
        [
            pytest.param(None, "deploy", id="first"),
            pytest.param({"sha": "old", "status": "deployed"}, "deploy", id="new-sha"),
            pytest.param({"sha": "new", "status": "deployed"}, "skip", id="same-sha"),
        ],
    )
    def test_no_ci_to_wait_for(self, state, expected):
        assert decide("new", state, None) == expected


def pr(number, merge_sha, merged=True):
    return {
        "number": number,
        "merge_commit_sha": merge_sha,
        "merged_at": "2026-09-28T12:00:00Z" if merged else None,
        "title": f"PR {number}",
        "html_url": f"https://github.com/Org/proj/pull/{number}",
    }


def change(number, *shas):
    return Change(number, f"#{number}", f"PR {number}", f"https://example.com/{number}", frozenset(shas))


def test_select_merged_keeps_changes_with_a_commit_in_the_range():
    changes = [change(1, "a"), change(2, "b"), change(3, "zzz"), change(4, "x", "a")]
    assert [item.number for item in select_merged(changes, {"a", "b"})] == [1, 2, 4]


API = "/api/v3/repos/Org/proj"


@pytest.fixture
def app_env(dokku_env, fake_api):
    """App proj-stg deploying develop of a GitHub repository served by `fake_api`, notifying both channels."""
    dokku_env.git_auth("127.0.0.1", "GH")
    dokku_env.configure(
        "proj-stg",
        repository=f"{fake_api.url}/Org/proj",
        forge="github",
        branch="develop",
        workflow=WORKFLOW,
        notify="comment,telegram",
        telegram_chat="-100_9",
    )
    dokku_env.configure(GLOBAL, telegram_bot_token="TG")
    return dokku_env


def github_state(fake_api, head="c3", runs=None, compare=("c2", "c3"), pulls=None):
    fake_api.routes[f"GET {API}/branches/develop"] = (200, {"commit": {"sha": head}})
    fake_api.routes[f"GET {API}/actions/runs"] = (200, {"workflow_runs": runs if runs is not None else [run()]})
    fake_api.routes[f"GET {API}/compare/c1...c3"] = (200, {"commits": [{"sha": sha} for sha in compare]})
    fake_api.routes[f"GET {API}/pulls"] = (200, pulls if pulls is not None else [pr(10, "c2"), pr(9, "c1")])
    fake_api.routes[f"POST {API}/issues/10/comments"] = (201, {})
    fake_api.routes["POST /botTG/sendMessage"] = (200, {"ok": True})


def write_state(env, **entry):
    save_state(env.properties, "proj-stg", entry)


def read_state(env):
    return load_state(env.properties, "proj-stg")


def do_poll(env, fake_api, output=None, redeploy=()):
    return poll(
        env.properties,
        redeploy=redeploy,
        telegram_api=fake_api.url,
        on_output=(output.append if output is not None else lambda chunk: None),
    )


def clone_url(fake_api):
    return f"{fake_api.url}/Org/proj.git"


class TestPoll:
    def test_green_ci_deploys_exact_sha_and_notifies(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(output="-----> Building\n")
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        output = []
        assert do_poll(app_env, fake_api, output) == 0
        assert fake_dokku.syncs == [f"git:sync --build proj-stg {clone_url(fake_api)} c3"]
        assert b"".join(output) == b"-----> Building\n"
        assert (read_state(app_env)["status"], read_state(app_env)["deployed_sha"]) == ("deployed", "c3")
        assert [path for path, _ in fake_api.posts()] == [f"{API}/issues/10/comments", "/botTG/sendMessage"]

    def test_empty_workflow_deploys_without_asking_for_ci_runs(self, app_env, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(status="in_progress", conclusion=None)])
        fake_dokku.set()
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        app_env.configure("proj-stg", notify="none", workflow="none")
        assert do_poll(app_env, fake_api) == 0
        assert fake_dokku.syncs == [f"git:sync --build proj-stg {clone_url(fake_api)} c3"]
        assert not [path for _, path, _, _ in fake_api.requests if path.endswith("/actions/runs")]

    def test_notifications_include_the_app_url(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(url="https://proj-stg.example.com")
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        do_poll(app_env, fake_api)
        comment, telegram = (body for _, body in fake_api.posts())
        assert "https://proj-stg.example.com" in comment["body"]
        assert "https://proj-stg.example.com" in telegram["text"]

    def test_waits_while_ci_runs(self, app_env, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(status="in_progress", conclusion=None)])
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(app_env, fake_api) == 0
        assert fake_dokku.syncs == []
        assert read_state(app_env)["sha"] == "c1"

    def test_red_ci_is_recorded_without_deploy_or_notification(self, app_env, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(conclusion="failure")])
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(app_env, fake_api) == 0
        assert fake_dokku.syncs == []
        assert read_state(app_env)["status"] == "ci_failed"
        assert fake_api.posts() == []

    def test_failed_deploy_is_reported_once_and_retried_only_with_redeploy(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(exit_code=1, output=" ! pip install failed\n")
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        app_env.configure("proj-stg", notify="comment")
        do_poll(app_env, fake_api)
        (comment,) = fake_api.posts()
        assert "pip install failed" in comment[1]["body"]
        assert read_state(app_env) | {"at": None} == {
            "sha": "c3",
            "status": "deploy_failed",
            "deployed_sha": "c1",
            "at": None,
        }
        do_poll(app_env, fake_api)
        assert len(fake_dokku.syncs) == 1
        fake_dokku.set(exit_code=0)
        do_poll(app_env, fake_api, redeploy=["proj-stg"])
        assert len(fake_dokku.syncs) == 2
        assert read_state(app_env)["status"] == "deployed"

    def test_locked_app_is_left_alone(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(locked=True)
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(app_env, fake_api) == 0
        assert fake_dokku.syncs == []
        assert read_state(app_env)["sha"] == "c1"
        assert fake_api.posts() == []

    def test_lock_taken_during_our_sync_is_not_our_failure(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(exit_code=1, lock_during_sync=True)
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(app_env, fake_api) == 0
        assert len(fake_dokku.syncs) == 1
        assert read_state(app_env)["sha"] == "c1"
        assert fake_api.posts() == []

    def test_redeploy_of_same_sha_skips_pr_comments_but_not_telegram(self, app_env, fake_api, fake_dokku):
        github_state(fake_api, pulls=[pr(10, "c3")])
        fake_dokku.set()
        write_state(app_env, sha="c3", status="deployed", deployed_sha="c3")
        do_poll(app_env, fake_api, redeploy=["proj-stg"])
        assert len(fake_dokku.syncs) == 1
        assert [path for path, _ in fake_api.posts()] == ["/botTG/sendMessage"]

    def test_first_deploy_without_state_notifies_only_the_head_pr(self, app_env, fake_api, fake_dokku):
        github_state(fake_api, pulls=[pr(10, "c3"), pr(9, "c1")])
        fake_dokku.set()
        app_env.configure("proj-stg", notify="comment")
        do_poll(app_env, fake_api)
        assert [path for path, _ in fake_api.posts()] == [f"{API}/issues/10/comments"]

    def test_commit_already_running_is_recorded_without_rebuilding(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(deploy_source="https://example.com/Org/proj.git#c3")
        assert do_poll(app_env, fake_api) == 0
        assert fake_dokku.syncs == []
        assert (read_state(app_env)["status"], read_state(app_env)["deployed_sha"]) == ("deployed", "c3")
        assert fake_api.posts() == []

    def test_redeploy_rebuilds_even_if_commit_is_already_running(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(deploy_source="https://example.com/Org/proj.git#c3")
        do_poll(app_env, fake_api, redeploy=["proj-stg"])
        assert len(fake_dokku.syncs) == 1

    def test_app_running_another_commit_is_deployed(self, app_env, fake_api, fake_dokku):
        """Regression: `GIT_REV` named the head after a failed manual deploy of it, which was then taken as deployed.
        Only the last successful deploy counts."""
        github_state(fake_api)
        fake_dokku.set(deploy_source="https://example.com/Org/proj.git#c1")
        app_env.configure("proj-stg", notify="none")
        do_poll(app_env, fake_api)
        assert len(fake_dokku.syncs) == 1

    def test_api_error_counts_as_error_and_keeps_state(self, app_env, fake_api, fake_dokku):
        write_state(app_env, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(app_env, fake_api) == 1
        assert fake_dokku.syncs == []
        assert read_state(app_env)["sha"] == "c1"


class TestPollApps:
    def test_apps_without_repository_are_ignored(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set()
        app_env.configure("other", branch="main")
        assert do_poll(app_env, fake_api) == 0
        assert [sync.split()[2] for sync in fake_dokku.syncs] == ["proj-stg"]

    def test_invalid_app_is_an_error_and_others_still_deploy(self, app_env, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set()
        app_env.configure("broken", repository=f"{fake_api.url}/Org/proj", forge="github")
        assert do_poll(app_env, fake_api) == 1
        assert [sync.split()[2] for sync in fake_dokku.syncs] == ["proj-stg"]

    def test_workflow_and_notify_fall_back_to_global(self, app_env, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(path=".github/workflows/global.yml")])
        fake_dokku.set()
        app_env.properties.delete("proj-stg", "workflow")
        app_env.properties.delete("proj-stg", "notify")
        app_env.configure(GLOBAL, workflow=".github/workflows/global.yml", notify="telegram")
        assert do_poll(app_env, fake_api) == 0
        assert len(fake_dokku.syncs) == 1
        assert [path for path, _ in fake_api.posts()] == ["/botTG/sendMessage"]

    def test_missing_forge_token_is_an_error_without_calling_the_api(self, app_env, fake_api, fake_dokku):
        (app_env.home / ".netrc").write_text("machine gitlab.com login bot password X\n")
        assert do_poll(app_env, fake_api) == 1
        assert fake_api.requests == []

    def test_api_calls_use_the_netrc_token(self, app_env, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(status="queued", conclusion=None)])
        do_poll(app_env, fake_api)
        assert fake_api.headers[0]["Authorization"] == "Bearer GH"


@pytest.mark.parametrize("value", ["{not json", "[]"])
def test_unreadable_state_starts_over(app_env, fake_api, fake_dokku, value):
    github_state(fake_api)
    fake_dokku.set(deploy_source="https://example.com/Org/proj.git#c3")
    app_env.properties.set("proj-stg", "state", value)
    assert do_poll(app_env, fake_api) == 0
    assert fake_dokku.syncs == []
    assert read_state(app_env)["status"] == "deployed"
