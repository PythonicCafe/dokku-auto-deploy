import logging

from dokku_auto_deploy.forge import Change
from dokku_auto_deploy.github import GitHub
from dokku_auto_deploy.notify import (
    TELEGRAM_MAX_LENGTH,
    DeployResult,
    comment_body,
    error_tail,
    notify,
    telegram_text,
)
from dokku_auto_deploy.repository import Repository
from dokku_auto_deploy.settings import AppConfig

SHA = "abcdef1234567890"


REPOSITORY = Repository("https://github.com/Org/proj", "github")


def make_target(notify=(), telegram_chat=None):
    return AppConfig(
        app="proj-stg",
        repository=REPOSITORY,
        branch="develop",
        workflow=".github/workflows/ci.yml",
        notify=tuple(notify),
        telegram_chat=telegram_chat,
    )


def github(api_url=None):
    """GitHub client for `REPOSITORY`, talking to `api_url` (the fake API) instead of api.github.com."""
    client = GitHub(REPOSITORY, "gh")
    if api_url:
        client.api = api_url
    return client


APP_URL = "https://proj-stg.example.com"


def make_result(success=True, output="", changes=(), target=None, app_url=APP_URL):
    return DeployResult(
        target=target or make_target(),
        sha=SHA,
        success=success,
        output=output,
        changes=list(changes),
        commit_url=f"https://github.com/Org/proj/commit/{SHA}",
        app_url=app_url,
    )


def pull(number, title="Adds <export> & more"):
    return Change(number, f"#{number}", title, f"https://github.com/Org/proj/pull/{number}", frozenset({"m"}))


PR_7 = pull(7)


class TestErrorTail:
    def test_keeps_last_lines_and_strips_ansi(self):
        output = "".join(f"line {number}\n" for number in range(100)) + "\x1b[31m ! error here\x1b[0m\n"
        assert error_tail(output, max_lines=3) == "line 98\nline 99\n ! error here"

    def test_escapes_code_fence(self):
        assert "```" not in error_tail("before ``` after\n", max_lines=5)


class TestGitHubComment:
    def test_success_links_the_commit_and_shows_the_app_url(self):
        body = comment_body(make_result(success=True))
        assert f"[`abcdef12`](https://github.com/Org/proj/commit/{SHA})" in body
        assert "`proj-stg`" in body and "succeeded" in body
        assert f"[{APP_URL}]({APP_URL})" in body

    def test_without_app_url(self):
        body = comment_body(make_result(success=True, app_url=None))
        assert "https://proj-stg" not in body and "succeeded" in body

    def test_failure_includes_log_and_retry_command(self):
        body = comment_body(make_result(success=False, output="step 1\n ! boom\n"))
        assert " ! boom" in body
        assert "dokku auto-deploy:poll --redeploy proj-stg" in body


class TestTelegram:
    def test_links(self):
        text = telegram_text(make_result(changes=[PR_7]))
        assert f'<a href="https://github.com/Org/proj/commit/{SHA}">commit</a>' in text
        assert '<a href="https://github.com/Org/proj/pull/7">#7 Adds &lt;export&gt; &amp; more</a>' in text
        assert f'<a href="{APP_URL}">{APP_URL}</a>' in text

    def test_without_app_url(self):
        assert "proj-stg.example.com" not in telegram_text(make_result(app_url=None))

    def test_escapes_html_from_titles(self):
        assert "Adds &lt;export&gt; &amp; more" in telegram_text(make_result(changes=[PR_7]))

    def test_failure_log_is_escaped_truncated_and_keeps_the_end(self):
        output = "<script>\n" + ("x" * 200 + "\n") * 100 + "FINAL ERROR\n"
        text = telegram_text(make_result(success=False, output=output))
        assert "<script>" not in text
        assert "FINAL ERROR" in text
        assert len(text) <= TELEGRAM_MAX_LENGTH


class TestNotify:
    def test_github_comments_on_each_merged_pr(self, fake_api):
        fake_api.routes["POST /repos/Org/proj/issues/7/comments"] = (201, {})
        fake_api.routes["POST /repos/Org/proj/issues/8/comments"] = (201, {})
        result = make_result(changes=[PR_7, pull(8)], target=make_target(notify=["comment"]))
        failures = notify(result, github(fake_api.url), telegram_token=None)
        assert failures == []
        assert [path for path, _ in fake_api.posts()] == [
            "/repos/Org/proj/issues/7/comments",
            "/repos/Org/proj/issues/8/comments",
        ]

    def test_github_without_prs_sends_nothing(self, fake_api):
        result = make_result(changes=[], target=make_target(notify=["comment"]))
        assert notify(result, github(fake_api.url), telegram_token=None) == []
        assert fake_api.posts() == []

    def test_telegram_sends_to_topic(self, fake_api):
        fake_api.routes["POST /botTOKEN/sendMessage"] = (200, {"ok": True})
        result = make_result(target=make_target(notify=["telegram"], telegram_chat="-100_29"))
        assert notify(result, github(), telegram_token="TOKEN", telegram_api=fake_api.url) == []
        ((path, fields),) = fake_api.posts()
        assert path == "/botTOKEN/sendMessage"
        assert (fields["chat_id"], fields["message_thread_id"], fields["parse_mode"]) == ("-100", "29", "HTML")

    def test_failing_channel_does_not_stop_the_others_and_hides_the_token(self, fake_api):
        fake_api.routes["POST /botTOKEN/sendMessage"] = (400, {"ok": False, "description": "chat not found"})
        fake_api.routes["POST /repos/Org/proj/issues/7/comments"] = (201, {})
        target = make_target(notify=["telegram", "comment"], telegram_chat="-100")
        failures = notify(
            make_result(changes=[PR_7], target=target),
            github(fake_api.url),
            telegram_token="TOKEN",
            telegram_api=fake_api.url,
        )
        assert len(failures) == 1
        assert "chat not found" in failures[0] and "TOKEN" not in failures[0]
        assert [path for path, _ in fake_api.posts("/repos")] == ["/repos/Org/proj/issues/7/comments"]

    def test_telegram_without_token_is_a_failure(self):
        target = make_target(notify=["telegram"], telegram_chat="-100")
        (failure,) = notify(make_result(target=target), github(), telegram_token=None)
        assert "token" in failure


class TestNotifyLog:
    def test_logs_each_github_comment(self, fake_api, caplog):
        fake_api.routes["POST /repos/Org/proj/issues/7/comments"] = (201, {})
        caplog.set_level(logging.INFO)
        notify(
            make_result(changes=[PR_7], target=make_target(notify=["comment"])),
            github(fake_api.url),
            None,
        )
        assert "[proj-stg] GitHub: commented on #7" in caplog.messages

    def test_logs_why_github_did_not_comment(self, caplog):
        caplog.set_level(logging.INFO)
        notify(make_result(changes=[], target=make_target(notify=["comment"])), github(), None)
        assert "[proj-stg] GitHub: no merged change in this deploy, nothing to comment" in caplog.messages

    def test_logs_telegram_message(self, fake_api, caplog):
        fake_api.routes["POST /botTOKEN/sendMessage"] = (200, {"ok": True})
        caplog.set_level(logging.INFO)
        target = make_target(notify=["telegram"], telegram_chat="-100_29")
        notify(make_result(target=target), github(), "TOKEN", telegram_api=fake_api.url)
        assert "[proj-stg] Telegram: message sent to -100_29" in caplog.messages
