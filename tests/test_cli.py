import argparse
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from dokku_auto_deploy import __version__
from dokku_auto_deploy.cli import main, parse_args, parse_change_number
from dokku_auto_deploy.poll import load_state, poll_lock, save_state
from dokku_auto_deploy.properties import GLOBAL, data_dir

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/ci.yml"
API = "/api/v3/repos/Org/proj"


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_plugin_toml_version_matches_the_package():
    plugin = tomllib.loads((ROOT / "plugin.toml").read_text())
    assert plugin["plugin"]["version"] == __version__


class TestParsing:
    @pytest.mark.parametrize("value, expected", [("12", 12), ("#12", 12), ("!12", 12), (" 7 ", 7)])
    def test_change_number(self, value, expected):
        assert parse_change_number(value) == expected

    @pytest.mark.parametrize("value", ["", "0", "-1", "abc", "https://github.com/Org/proj/pull/1"])
    def test_invalid_change_number(self, value):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_change_number(value)

    def test_global_is_a_positional_value(self):
        args = parse_args(["auto-deploy:set", "--global", "workflow", "none"])
        assert (args.target, args.key, args.value) == (GLOBAL, "workflow", "none")
        assert parse_args(["auto-deploy:report", "--global"]).target == GLOBAL

    def test_help_still_works_for_positional_only_commands(self, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(["auto-deploy:set", "--help"])
        assert exc.value.code == 0
        assert "telegram-bot-token" in capsys.readouterr().out

    def test_negative_chat_id_is_a_value(self):
        assert parse_args(["auto-deploy:set", "app", "telegram-chat", "-1001234_5"]).value == "-1001234_5"

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["auto-deploy:bogus"], id="unknown-command"),
            pytest.param(["auto-deploy:set", "app", "brnach", "main"], id="unknown-key"),
            pytest.param(["auto-deploy:set", "app"], id="missing-key"),
            pytest.param(["auto-deploy:notify-test"], id="missing-app"),
            pytest.param(["auto-deploy:notify-test", "app", "-p", "x"], id="bad-number"),
            pytest.param(["auto-deploy:poll", "--force", "app"], id="force-is-not-an-option"),
        ],
    )
    def test_invalid_arguments_exit_2(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args(argv)
        assert exc.value.code == 2
        assert capsys.readouterr().err.startswith("usage: dokku ")


class TestSet:
    def test_sets_and_unsets(self, dokku_env, fake_dokku, capsys):
        assert main(["auto-deploy:set", "app", "repository", "https://github.com/Org/proj.git"]) == 0
        assert dokku_env.properties.get("app", "repository") == "https://github.com/Org/proj"
        assert capsys.readouterr().out == "=====> Setting repository to https://github.com/Org/proj\n"
        assert main(["auto-deploy:set", "app", "repository"]) == 0
        assert dokku_env.properties.get("app", "repository") is None
        assert capsys.readouterr().out == "=====> Unsetting repository\n"

    def test_global(self, dokku_env, fake_dokku):
        assert main(["auto-deploy:set", "--global", "notify", "telegram"]) == 0
        assert dokku_env.properties.get(GLOBAL, "notify") == "telegram"
        assert fake_dokku.calls == []

    @pytest.mark.parametrize(
        "argv, message",
        [
            pytest.param(["app", "notify", "email"], "invalid notify", id="invalid-value"),
            pytest.param([GLOBAL, "branch", "main"], "only be set per app", id="app-key-globally"),
            pytest.param(["app", "telegram-bot-token"], "only be set with --global", id="global-key-per-app"),
            pytest.param(["gone", "branch", "main"], "app gone does not exist", id="missing-app"),
        ],
    )
    def test_rejected_without_writing(self, dokku_env, fake_dokku, capsys, argv, message):
        fake_dokku.remove_app("gone")
        assert main(["auto-deploy:set", *argv]) == 3
        assert message in capsys.readouterr().err
        assert not dokku_env.properties.root.exists()

    def test_secret_as_argument_is_refused(self, dokku_env, fake_dokku, capsys):
        assert main(["auto-deploy:set", "--global", "telegram-bot-token", "123:ABC"]) == 2
        assert "stdin" in capsys.readouterr().err
        assert dokku_env.properties.get(GLOBAL, "telegram-bot-token") is None


def dokku_subcommand(command):
    """The script Dokku 0.38 runs for `command` (`execute_dokku_cmd` in the dokku script): for a community plugin,
    `subcommands/default` for the bare plugin name, else `subcommands/<part after the colon>`."""
    plugin, _, name = command.partition(":")
    assert plugin == "auto-deploy"
    return f"subcommands/{name or 'default'}"


def run_plugin(dokku_env, *args, stdin=None, script=None):
    """Run a plugin script the way Dokku does (bash, as a separate process, with Dokku's environment variables); by
    default, the subcommand script Dokku would pick for `args[0]`."""
    script = script or dokku_subcommand(args[0])
    fake_bin = dokku_env.home / "bin"
    fake_bin.mkdir(exist_ok=True)
    column = fake_bin / "column"  # Not installed everywhere; alignment doesn't matter here
    column.write_text("#!/bin/sh\ncat\n")
    column.chmod(0o755)
    env = {
        **os.environ,
        "DOKKU_LIB_ROOT": str(dokku_env.lib_root),
        "DOKKU_ROOT": str(dokku_env.home),
        "DOKKU_NOT_IMPLEMENTED_EXIT": "10",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", str(ROOT / script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


class TestPluginScripts:
    def test_every_command_has_the_script_dokku_looks_for(self):
        commands = re.findall(r"^    (auto-deploy:[\w-]+)", (ROOT / "help-functions").read_text(), re.MULTILINE)
        assert len(commands) >= 4
        for command in [*commands, "auto-deploy", "auto-deploy:help"]:
            script = ROOT / dokku_subcommand(command)
            assert script.is_file() and os.access(script, os.X_OK), command

    def test_secret_is_read_from_stdin_and_never_printed(self, dokku_env, fake_dokku):
        result = run_plugin(dokku_env, "auto-deploy:set", "--global", "telegram-bot-token", stdin="123:SECRET\n")
        assert result.returncode == 0, result.stderr
        assert "SECRET" not in result.stdout + result.stderr
        assert dokku_env.properties.get(GLOBAL, "telegram-bot-token") == "123:SECRET"

    def test_secret_from_a_file_redirect(self, dokku_env, fake_dokku, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("123:SECRET\n")
        with token_file.open() as stdin:
            result = subprocess.run(
                ["bash", str(ROOT / "subcommands/set"), "auto-deploy:set", "--global", "telegram-bot-token"],
                stdin=stdin,
                capture_output=True,
                text=True,
                env={**os.environ, "DOKKU_LIB_ROOT": str(dokku_env.lib_root)},
                timeout=30,
                check=False,
            )
        assert result.returncode == 0, result.stderr
        assert dokku_env.properties.get(GLOBAL, "telegram-bot-token") == "123:SECRET"

    def test_empty_stdin_keeps_the_secret(self, dokku_env, fake_dokku):
        dokku_env.configure(GLOBAL, telegram_bot_token="123:SECRET")
        result = run_plugin(dokku_env, "auto-deploy:set", "--global", "telegram-bot-token", stdin="")
        assert result.returncode == 3
        assert "empty telegram-bot-token on stdin" in result.stderr
        assert dokku_env.properties.get(GLOBAL, "telegram-bot-token") == "123:SECRET"

    def test_invalid_secret_is_not_echoed(self, dokku_env, fake_dokku):
        result = run_plugin(dokku_env, "auto-deploy:set", "--global", "telegram-bot-token", stdin="123:AB\nSECRET\n")
        assert result.returncode == 3
        assert "SECRET" not in result.stdout + result.stderr
        assert dokku_env.properties.get(GLOBAL, "telegram-bot-token") is None

    def test_current_directory_is_not_on_the_import_path(self, dokku_env, fake_dokku, tmp_path):
        (tmp_path / "argparse.py").write_text("raise SystemExit('hijacked')\n")
        result = subprocess.run(
            ["bash", str(ROOT / "subcommands/report"), "auto-deploy:report", "--global"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**os.environ, "DOKKU_LIB_ROOT": str(dokku_env.lib_root), "PYTHONPATH": str(tmp_path)},
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "hijacked" not in result.stderr

    @pytest.mark.parametrize("command", ["auto-deploy", "auto-deploy:help"])
    def test_help(self, dokku_env, command):
        result = run_plugin(dokku_env, command)
        assert result.returncode == 0
        assert result.stdout.startswith("Usage: dokku auto-deploy[:COMMAND]")
        for name in ("notify-test", "poll", "report", "schedule", "set"):
            assert f"auto-deploy:{name} " in result.stdout

    def test_dokku_help_lists_the_plugin(self, dokku_env):
        result = run_plugin(dokku_env, "help", script="commands")
        assert result.returncode == 0
        assert result.stdout.strip().startswith("auto-deploy, ")

    def test_other_commands_are_not_implemented_here(self, dokku_env):
        assert run_plugin(dokku_env, "apps:list", script="commands").returncode == 10

    def test_argparse_usage_names_the_dokku_command(self, dokku_env):
        result = run_plugin(dokku_env, "auto-deploy:poll", "--help")
        assert result.returncode == 0
        assert result.stdout.startswith("usage: dokku auto-deploy:poll [-h] [-r app] [-v]")


class TestReport:
    def configure(self, dokku_env):
        dokku_env.configure(
            "proj-stg", repository="https://github.com/Org/proj", branch="develop", telegram_chat="-100_9"
        )
        dokku_env.configure(GLOBAL, workflow=WORKFLOW, notify="telegram", telegram_bot_token="123:SECRET")
        save_state(
            dokku_env.properties, "proj-stg", {"sha": "c3", "status": "deployed", "at": "T", "deployed_sha": "c3"}
        )

    def test_everything(self, dokku_env, fake_dokku, capsys):
        self.configure(dokku_env)
        fake_dokku.set_timer("enabled")
        dokku_env.configure("not-configured", branch="main")
        assert main(["auto-deploy:report"]) == 0
        out = capsys.readouterr().out
        assert "SECRET" not in out
        assert [" ".join(line.split()) for line in out.splitlines()] == [
            "=====> auto-deploy global settings",
            f"workflow: {WORKFLOW}",
            "notify: telegram",
            "telegram-chat:",
            "telegram-bot-token: set",
            "cron: off",
            "systemd timer: enabled",
            "=====> proj-stg auto-deploy information",
            "repository: https://github.com/Org/proj",
            "branch: develop",
            "forge:",
            f"workflow: {WORKFLOW} (global)",
            "notify: telegram (global)",
            "telegram-chat: -100_9",
            "last handled: c3 deployed T",
            "last deployed: c3",
        ]

    def test_problem_is_shown(self, dokku_env, fake_dokku, capsys):
        self.configure(dokku_env)
        dokku_env.properties.delete(GLOBAL, "workflow")
        assert main(["auto-deploy:report", "proj-stg"]) == 0
        lines = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
        assert "problem: workflow is not set (dokku auto-deploy:set <app>|--global workflow" in "\n".join(lines)

    def test_unconfigured_app(self, dokku_env, capsys):
        assert main(["auto-deploy:report", "nope"]) == 3
        assert "not configured for nope" in capsys.readouterr().err


@pytest.fixture
def app_env(dokku_env, fake_api, monkeypatch):
    dokku_env.git_auth("127.0.0.1", "GH")
    dokku_env.configure(
        "proj-stg",
        repository=f"{fake_api.url}/Org/proj",
        forge="github",
        branch="develop",
        workflow="none",
        notify="telegram",
        telegram_chat="-100_9",
    )
    dokku_env.configure(GLOBAL, telegram_bot_token="TG")
    monkeypatch.setenv("DOKKU_AUTO_DEPLOY_TELEGRAM_API", fake_api.url)
    fake_api.routes["POST /botTG/sendMessage"] = (200, {"ok": True})
    fake_api.routes[f"POST {API}/issues/5/comments"] = (201, {})
    return dokku_env


class TestPoll:
    def test_deploys_and_streams_the_build_output(self, app_env, fake_api, fake_dokku, capfd):
        fake_api.routes[f"GET {API}/branches/develop"] = (200, {"commit": {"sha": "c3"}})
        fake_dokku.set(output="-----> Building\n")
        assert main(["auto-deploy:poll"]) == 0
        assert capfd.readouterr().out == "-----> Building\n"
        assert load_state(app_env.properties, "proj-stg")["deployed_sha"] == "c3"

    def test_error_in_an_app_exits_1(self, app_env, fake_api, fake_dokku):
        assert main(["auto-deploy:poll"]) == 1

    def test_redeploy_while_another_poll_runs_is_an_error(self, app_env, fake_api, capsys):
        with poll_lock(data_dir() / "poll.lock"):
            assert main(["auto-deploy:poll", "--redeploy", "proj-stg"]) == 1
        assert "another auto-deploy:poll is running; run the redeploy again" in capsys.readouterr().err
        assert fake_api.requests == []

    def test_redeploy_of_unconfigured_app(self, app_env, capsys):
        assert main(["auto-deploy:poll", "--redeploy", "nope"]) == 3
        assert "not configured for: nope" in capsys.readouterr().err

    def test_running_poll_makes_the_next_one_skip(self, app_env, fake_api, capsys):
        with poll_lock(data_dir() / "poll.lock"):
            assert main(["auto-deploy:poll"]) == 0
        assert fake_api.requests == []
        assert "INFO another auto-deploy:poll is running, skipping this run" in capsys.readouterr().err


class TestNotifyTest:
    def test_telegram(self, app_env, fake_api, capsys):
        assert main(["auto-deploy:notify-test", "proj-stg"]) == 0
        assert capsys.readouterr().out == "ok telegram -100_9\n"
        ((path, fields),) = fake_api.posts()
        assert (path, fields["chat_id"], fields["message_thread_id"]) == ("/botTG/sendMessage", "-100", "9")
        assert "proj-stg" in fields["text"]

    def test_comment(self, app_env, fake_api, capsys):
        assert main(["auto-deploy:notify-test", "proj-stg", "-p", "#5"]) == 0
        assert f"ok comment {fake_api.url}/Org/proj 5" in capsys.readouterr().out
        assert [path for path, _ in fake_api.posts()] == ["/botTG/sendMessage", f"{API}/issues/5/comments"]

    def test_failure_exits_1(self, app_env, fake_api, capsys):
        fake_api.routes["POST /botTG/sendMessage"] = (400, {"ok": False, "description": "chat not found"})
        assert main(["auto-deploy:notify-test", "proj-stg"]) == 1
        assert "chat not found" in capsys.readouterr().err

    def test_nothing_to_test(self, app_env, capsys):
        app_env.configure("proj-stg", notify="comment")
        assert main(["auto-deploy:notify-test", "proj-stg"]) == 3
        assert "--pull-request" in capsys.readouterr().err

    def test_invalid_settings_exit_3(self, app_env, capsys):
        app_env.properties.delete("proj-stg", "telegram-chat")
        assert main(["auto-deploy:notify-test", "proj-stg"]) == 3
        assert "telegram-chat is not set" in capsys.readouterr().err


def normalized(out):
    return [" ".join(line.split()) for line in out.splitlines()]


class TestSchedule:
    def test_status(self, dokku_env, fake_dokku, capsys, monkeypatch):
        monkeypatch.setenv("DOKKU_LOGS_DIR", "/var/log/dokku")
        fake_dokku.set_timer("disabled")
        dokku_env.configure(GLOBAL, schedule="cron")
        assert main(["auto-deploy:schedule"]) == 0
        assert normalized(capsys.readouterr().out) == [
            "=====> auto-deploy schedule",
            "cron: on, every minute, log: /var/log/dokku/auto-deploy.log",
            "systemd timer: disabled",
        ]

    def test_cron_regenerates_the_crontab(self, dokku_env, fake_dokku, capsys):
        fake_dokku.set_timer("disabled")
        assert main(["auto-deploy:schedule", "cron"]) == 0
        assert dokku_env.properties.get(GLOBAL, "schedule") == "cron"
        assert [call for call in fake_dokku.calls if call.startswith("plugn")] == ["plugn trigger scheduler-cron-write"]
        assert capsys.readouterr().out == "=====> cron on\n"

    def test_cron_is_undone_when_dokku_does_not_write_the_task(self, dokku_env, fake_dokku, capsys):
        """Like a server whose apps all run on k3s: Dokku leaves the host crontab alone."""
        fake_dokku.set_timer("disabled")
        (fake_dokku.directory / "no-host-cron").touch()
        assert main(["auto-deploy:schedule", "cron"]) == 1
        assert "use the systemd timer instead" in capsys.readouterr().err
        assert dokku_env.properties.get(GLOBAL, "schedule") is None
        assert fake_dokku.calls.count("plugn trigger scheduler-cron-write") == 2

    def test_failure_to_write_the_crontab_is_reported_and_can_be_retried(self, dokku_env, fake_dokku, capsys):
        fake_dokku.set_timer("disabled")
        plugn = fake_dokku.directory.parent / "bin" / "plugn"
        working = plugn.read_text()
        plugn.write_text("#!/bin/sh\nexit 1\n")
        assert main(["auto-deploy:schedule", "cron"]) == 1
        assert "could not regenerate the crontab" in capsys.readouterr().err
        plugn.write_text(working)
        assert main(["auto-deploy:schedule", "cron"]) == 0
        assert "plugn trigger scheduler-cron-write" in fake_dokku.calls

    def test_cron_with_timer_enabled_says_how_to_disable_it(self, dokku_env, fake_dokku, capsys):
        fake_dokku.set_timer("enabled")
        assert main(["auto-deploy:schedule", "cron"]) == 0
        assert "systemctl disable --now dokku-auto-deploy.timer" in capsys.readouterr().out

    @pytest.mark.parametrize("scheduler", ["systemd", "none"])
    def test_systemd_and_none_remove_the_cron_task(self, dokku_env, fake_dokku, scheduler):
        fake_dokku.set_timer("disabled")
        dokku_env.configure(GLOBAL, schedule="cron")
        assert main(["auto-deploy:schedule", scheduler]) == 0
        assert dokku_env.properties.get(GLOBAL, "schedule") is None
        assert "plugn trigger scheduler-cron-write" in fake_dokku.calls

    def test_systemd_says_how_to_enable_the_timer(self, dokku_env, fake_dokku, capsys):
        fake_dokku.set_timer("disabled")
        assert main(["auto-deploy:schedule", "systemd"]) == 0
        assert "systemctl enable --now dokku-auto-deploy.timer" in capsys.readouterr().out

    def test_systemd_without_the_units_changes_nothing(self, dokku_env, fake_dokku, capsys):
        dokku_env.configure(GLOBAL, schedule="cron")
        assert main(["auto-deploy:schedule", "systemd"]) == 3
        assert "use cron instead" in capsys.readouterr().err
        assert dokku_env.properties.get(GLOBAL, "schedule") == "cron"

    def test_schedule_is_not_a_setting(self, dokku_env, fake_dokku):
        with pytest.raises(SystemExit):
            parse_args(["auto-deploy:set", "--global", "schedule", "cron"])


class TestUninstallTrigger:
    @staticmethod
    def start(dokku_env):
        """Start the trigger with stand-ins for Dokku's property functions that use Dokku's property layout."""
        core = dokku_env.home / "core-plugins"
        (core / "common").mkdir(parents=True)
        (core / "common" / "property-functions").write_text(
            'fn-plugin-property-get() { cat "$DOKKU_LIB_ROOT/config/$1/$2/$3" 2>/dev/null || true; }\n'
            'fn-plugin-property-delete() { rm -f "$DOKKU_LIB_ROOT/config/$1/$2/$3"; }\n'
        )
        env = {**os.environ, "DOKKU_LIB_ROOT": str(dokku_env.lib_root), "PLUGIN_CORE_AVAILABLE_PATH": str(core)}
        return subprocess.Popen(
            ["bash", str(ROOT / "uninstall"), "auto-deploy"], stdout=subprocess.PIPE, text=True, env=env
        )

    def test_waits_for_a_running_poll_after_removing_the_cron_task(self, dokku_env, fake_dokku):
        dokku_env.configure(GLOBAL, schedule="cron")
        with poll_lock(data_dir() / "poll.lock"):
            uninstall = self.start(dokku_env)
            assert "Waiting for the running auto-deploy:poll" in uninstall.stdout.readline()
            assert uninstall.poll() is None
            assert "plugn trigger scheduler-cron-write" in fake_dokku.calls  # No new poll while waiting
        assert uninstall.wait(timeout=30) == 0
        assert dokku_env.properties.get(GLOBAL, "schedule") is None

    def test_does_not_wait_when_no_poll_runs(self, dokku_env, fake_dokku):
        with poll_lock(data_dir() / "poll.lock"):
            pass  # Leaves the lock file, as every past poll does
        uninstall = self.start(dokku_env)
        output, _ = uninstall.communicate(timeout=30)
        assert uninstall.returncode == 0 and output == ""


class TestCronEntriesTrigger:
    """Runs the trigger with a stand-in for Dokku's property functions that reads Dokku's property layout."""

    def run(self, dokku_env, scheduler):
        core = dokku_env.home / "core-plugins"
        (core / "common").mkdir(parents=True)
        (core / "common" / "property-functions").write_text(
            'fn-plugin-property-get() { cat "$DOKKU_LIB_ROOT/config/$1/$2/$3" 2>/dev/null || true; }\n'
        )
        env = {
            **os.environ,
            "DOKKU_LIB_ROOT": str(dokku_env.lib_root),
            "PLUGIN_CORE_AVAILABLE_PATH": str(core),
            "DOKKU_LOGS_DIR": "/var/log/dokku",
        }
        result = subprocess.run(
            ["bash", str(ROOT / "cron-entries"), scheduler], capture_output=True, text=True, env=env, timeout=30
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_task_when_schedule_is_cron(self, dokku_env):
        dokku_env.configure(GLOBAL, schedule="cron")
        assert self.run(dokku_env, "docker-local") == (
            "* * * * *;dokku auto-deploy:poll;/var/log/dokku/auto-deploy.log\n"
        )

    def test_nothing_otherwise(self, dokku_env):
        assert self.run(dokku_env, "docker-local") == ""

    def test_nothing_for_other_schedulers(self, dokku_env):
        dokku_env.configure(GLOBAL, schedule="cron")
        assert self.run(dokku_env, "k3s") == ""
