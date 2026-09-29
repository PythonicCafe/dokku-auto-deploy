import json

import pytest

from dokku_auto_deploy.config import parse_config
from dokku_auto_deploy.poll import decide, poll, select_merged_prs

WORKFLOW = ".github/workflows/ci.yml"


def run(conclusion="success", status="completed", path=WORKFLOW, run_id=1):
    return {"id": run_id, "path": path, "status": status, "conclusion": conclusion}


class TestDecide:
    @pytest.mark.parametrize(
        "head, runs, state, expected",
        [
            pytest.param("abc", [run()], {"sha": "abc", "status": "deployed"}, "skip", id="already-deployed"),
            pytest.param("abc", [run()], {"sha": "abc", "status": "deploy_failed"}, "skip", id="failed-not-retried"),
            pytest.param("new", [run()], {"sha": "old", "status": "deployed"}, "deploy", id="new-green"),
            pytest.param("new", [run()], None, "deploy", id="first-deploy"),
            pytest.param("new", [], None, "wait", id="ci-not-started"),
            pytest.param("new", [run(status="in_progress", conclusion=None)], None, "wait", id="ci-running"),
            pytest.param("new", [run(conclusion="failure")], None, "ci_failed", id="ci-failed"),
            pytest.param("new", [run(path=".github/workflows/other.yml")], None, "wait", id="other-workflow"),
            pytest.param(
                "new", [run(conclusion="failure", run_id=1), run(run_id=2)], None, "deploy", id="rerun-green-wins"
            ),
            pytest.param(
                "new",
                [run(conclusion="failure", run_id=1), run(status="queued", conclusion=None, run_id=2)],
                None,
                "wait",
                id="rerun-running",
            ),
        ],
    )
    def test_decide(self, head, runs, state, expected):
        assert decide(head, runs, state, WORKFLOW) == expected

    @pytest.mark.parametrize(
        "state, expected",
        [
            pytest.param(None, "deploy", id="first"),
            pytest.param({"sha": "old", "status": "deployed"}, "deploy", id="new-sha"),
            pytest.param({"sha": "new", "status": "deployed"}, "skip", id="same-sha"),
        ],
    )
    def test_empty_workflow_ignores_ci(self, state, expected):
        assert decide("new", [run(conclusion="failure")], state, "") == expected


def pr(number, merge_sha, merged=True):
    return {
        "number": number,
        "merge_commit_sha": merge_sha,
        "merged_at": "2026-09-28T12:00:00Z" if merged else None,
        "title": f"PR {number}",
        "html_url": f"https://github.com/Org/proj/pull/{number}",
    }


def test_select_merged_prs_in_range():
    pulls = [pr(1, "a"), pr(2, "b"), pr(3, "zzz"), pr(4, "a", merged=False)]
    assert [item["number"] for item in select_merged_prs(pulls, {"a", "b"})] == [1, 2]


def make_config(tmp_path, notify='["github", "telegram"]', workflow=WORKFLOW):
    return parse_config(
        {
            "settings": {"state-file": str(tmp_path / "state.json")},
            "repo": [
                {
                    "repository": "Org/proj",
                    "workflow": workflow,
                    "notify": json.loads(notify),
                    "telegram-chat": "-100_9",
                    "stg": {},
                }
            ],
        }
    )


def github_state(fake_api, head="c3", runs=None, compare=("c2", "c3"), pulls=None):
    fake_api.routes["GET /repos/Org/proj/branches/develop"] = (200, {"commit": {"sha": head}})
    fake_api.routes["GET /repos/Org/proj/actions/runs"] = (
        200,
        {"workflow_runs": runs if runs is not None else [run()]},
    )
    fake_api.routes["GET /repos/Org/proj/compare/c1...c3"] = (200, {"commits": [{"sha": sha} for sha in compare]})
    fake_api.routes["GET /repos/Org/proj/pulls"] = (200, pulls if pulls is not None else [pr(10, "c2"), pr(9, "c1")])
    fake_api.routes["POST /repos/Org/proj/issues/10/comments"] = (201, {})
    fake_api.routes["POST /botTG/sendMessage"] = (200, {"ok": True})


def write_state(tmp_path, **entry):
    (tmp_path / "state.json").write_text(json.dumps({"proj-stg": entry}))


def read_state(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())["proj-stg"]


def do_poll(config, fake_api, output=None, force=()):
    return poll(
        config,
        github_token="GH",
        telegram_token="TG",
        force_apps=force,
        github_api=fake_api.url,
        telegram_api=fake_api.url,
        on_output=(output.append if output is not None else lambda chunk: None),
    )


class TestPoll:
    def test_green_ci_deploys_exact_sha_and_notifies(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(output="-----> Building\n")
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        output = []
        assert do_poll(make_config(tmp_path), fake_api, output) == 0
        assert fake_dokku.syncs == ["git:sync --build proj-stg https://github.com/Org/proj.git c3"]
        assert b"".join(output) == b"-----> Building\n"
        assert (read_state(tmp_path)["status"], read_state(tmp_path)["deployed_sha"]) == ("deployed", "c3")
        assert [path for path, _ in fake_api.posts()] == ["/repos/Org/proj/issues/10/comments", "/botTG/sendMessage"]

    def test_empty_workflow_deploys_without_asking_for_ci_runs(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(status="in_progress", conclusion=None)])
        fake_dokku.set()
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(make_config(tmp_path, notify="[]", workflow=""), fake_api) == 0
        assert fake_dokku.syncs == ["git:sync --build proj-stg https://github.com/Org/proj.git c3"]
        assert not [path for _, path, _, _ in fake_api.requests if path.endswith("/actions/runs")]

    def test_waits_while_ci_runs(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(status="in_progress", conclusion=None)])
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(make_config(tmp_path), fake_api) == 0
        assert fake_dokku.syncs == []
        assert read_state(tmp_path)["sha"] == "c1"

    def test_red_ci_is_recorded_without_deploy_or_notification(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api, runs=[run(conclusion="failure")])
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(make_config(tmp_path), fake_api) == 0
        assert fake_dokku.syncs == []
        assert read_state(tmp_path)["status"] == "ci_failed"
        assert fake_api.posts() == []

    def test_failed_deploy_is_reported_once_and_retried_only_with_force(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(exit_code=1, output=" ! pip install failed\n")
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        config = make_config(tmp_path, notify='["github"]')
        do_poll(config, fake_api)
        (comment,) = fake_api.posts()
        assert "pip install failed" in comment[1]["body"]
        assert read_state(tmp_path) | {"at": None} == {
            "sha": "c3",
            "status": "deploy_failed",
            "deployed_sha": "c1",
            "at": None,
        }
        do_poll(config, fake_api)
        assert len(fake_dokku.syncs) == 1
        fake_dokku.set(exit_code=0)
        do_poll(config, fake_api, force=["proj-stg"])
        assert len(fake_dokku.syncs) == 2
        assert read_state(tmp_path)["status"] == "deployed"

    def test_locked_app_is_left_alone(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(locked=True)
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(make_config(tmp_path), fake_api) == 0
        assert fake_dokku.syncs == []
        assert read_state(tmp_path)["sha"] == "c1"
        assert fake_api.posts() == []

    def test_lock_taken_during_our_sync_is_not_our_failure(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(exit_code=1, lock_during_sync=True)
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(make_config(tmp_path), fake_api) == 0
        assert len(fake_dokku.syncs) == 1
        assert read_state(tmp_path)["sha"] == "c1"
        assert fake_api.posts() == []

    def test_forced_redeploy_of_same_sha_skips_pr_comments_but_not_telegram(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api, pulls=[pr(10, "c3")])
        fake_dokku.set()
        write_state(tmp_path, sha="c3", status="deployed", deployed_sha="c3")
        do_poll(make_config(tmp_path), fake_api, force=["proj-stg"])
        assert len(fake_dokku.syncs) == 1
        assert [path for path, _ in fake_api.posts()] == ["/botTG/sendMessage"]

    def test_first_deploy_without_state_notifies_only_the_head_pr(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api, pulls=[pr(10, "c3"), pr(9, "c1")])
        fake_api.routes["POST /repos/Org/proj/issues/10/comments"] = (201, {})
        fake_dokku.set()
        do_poll(make_config(tmp_path, notify='["github"]'), fake_api)
        assert [path for path, _ in fake_api.posts()] == ["/repos/Org/proj/issues/10/comments"]

    def test_commit_already_running_is_recorded_without_rebuilding(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(git_rev="c3")
        assert do_poll(make_config(tmp_path), fake_api) == 0
        assert fake_dokku.syncs == []
        assert (read_state(tmp_path)["status"], read_state(tmp_path)["deployed_sha"]) == ("deployed", "c3")
        assert fake_api.posts() == []

    def test_force_rebuilds_even_if_commit_is_already_running(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(git_rev="c3")
        do_poll(make_config(tmp_path), fake_api, force=["proj-stg"])
        assert len(fake_dokku.syncs) == 1

    def test_app_running_another_commit_is_deployed(self, tmp_path, fake_api, fake_dokku):
        github_state(fake_api)
        fake_dokku.set(git_rev="c1")
        do_poll(make_config(tmp_path, notify="[]"), fake_api)
        assert len(fake_dokku.syncs) == 1

    def test_api_error_counts_as_error_and_keeps_state(self, tmp_path, fake_api, fake_dokku):
        write_state(tmp_path, sha="c1", status="deployed", deployed_sha="c1")
        assert do_poll(make_config(tmp_path), fake_api) == 1
        assert fake_dokku.syncs == []
        assert read_state(tmp_path)["sha"] == "c1"
