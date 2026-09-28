import tomllib
from pathlib import Path

import pytest

from dokku_auto_deploy.config import CONFIG_TEMPLATE, ConfigError, load_config, parse_config

WORKFLOW = ".github/workflows/django.yml"


def parse(text: str):
    return parse_config(tomllib.loads(text))


def by_env(config):
    return {target.environment: target for target in config.targets}


class TestEnvironments:
    def test_defaults_to_gitflow_branches_and_suffixed_app_names(self):
        config = parse(f'[[repo]]\nrepository = "Org/meu-projeto"\nworkflow = "{WORKFLOW}"\nstg = {{}}\nprd = {{}}\n')
        targets = by_env(config)
        assert (targets["stg"].app, targets["stg"].branch) == ("meu-projeto-stg", "develop")
        assert (targets["prd"].app, targets["prd"].branch) == ("meu-projeto-prd", "main")

    def test_only_production(self):
        config = parse(f'[[repo]]\nrepository = "Org/site"\nworkflow = "{WORKFLOW}"\n[repo.prd]\n')
        assert [(target.environment, target.app) for target in config.targets] == [("prd", "site-prd")]

    def test_name_app_and_branch_are_configurable(self):
        config = parse(
            f"""
            [[repo]]
            repository = "Org/repositorio-com-nome-longo"
            name = "curto"
            workflow = "{WORKFLOW}"
            stg = {{ branch = "staging" }}
            prd = {{ app = "curto-producao" }}
            """
        )
        targets = by_env(config)
        assert (targets["stg"].app, targets["stg"].branch) == ("curto-stg", "staging")
        assert (targets["prd"].app, targets["prd"].branch) == ("curto-producao", "main")

    @pytest.mark.parametrize(
        "env_table",
        [
            pytest.param('prd = { app = "", branch = "" }', id="blank"),
            pytest.param("prd = {}", id="missing"),
        ],
    )
    def test_blank_values_behave_like_missing_ones(self, env_table):
        config = parse(f'[[repo]]\nrepository = "Org/x"\nname = ""\nworkflow = "{WORKFLOW}"\n{env_table}\n')
        (target,) = config.targets
        assert (target.app, target.branch) == ("x-prd", "main")


class TestDefaults:
    def test_workflow_and_telegram_chat_come_from_defaults_when_blank_or_missing(self):
        config = parse(
            f"""
            [defaults]
            workflow = "{WORKFLOW}"
            telegram-chat = "-100_7"

            [[repo]]
            repository = "Org/a"
            notify = ["telegram"]
            prd = {{}}

            [[repo]]
            repository = "Org/b"
            workflow = ""
            telegram-chat = "-200"
            notify = ["telegram"]
            prd = {{}}
            """
        )
        assert [(target.workflow, target.telegram_chat) for target in config.targets] == [
            (WORKFLOW, "-100_7"),
            (WORKFLOW, "-200"),
        ]

    def test_notify_defaults_to_nothing(self):
        (target,) = parse(f'[[repo]]\nrepository = "Org/a"\nworkflow = "{WORKFLOW}"\nprd = {{}}\n').targets
        assert target.notify == ()

    def test_settings_default_paths(self):
        settings = parse("").settings
        assert settings.state_file == Path("/var/lib/dokku-auto-deploy/state.json")
        assert settings.github_token_file == Path("/etc/dokku-auto-deploy/github-token")
        assert settings.telegram_token_file == Path("/etc/dokku-auto-deploy/telegram-token")

    def test_settings_accept_kebab_and_snake_case(self):
        settings = parse('[settings]\nstate-file = "/tmp/a.json"\ngithub_token_file = "/tmp/gh"\n').settings
        assert (settings.state_file, settings.github_token_file) == (Path("/tmp/a.json"), Path("/tmp/gh"))


INVALID_CONFIGS = [
    pytest.param('[[repo]]\nrepository = "Org/a"\nworkflow = "w"\n', "no environment", id="no-environment"),
    pytest.param('[[repo]]\nrepository = "Org/a"\nworkflow = "w"\ndev = {}\n', "dev", id="unknown-environment"),
    pytest.param('[[repo]]\nrepository = "a"\nworkflow = "w"\nprd = {}\n', "owner/name", id="bad-repository"),
    pytest.param('[[repo]]\nworkflow = "w"\nprd = {}\n', "owner/name", id="missing-repository"),
    pytest.param('[[repo]]\nrepository = "Org/a"\nprd = {}\n', "workflow", id="missing-workflow"),
    pytest.param(
        '[[repo]]\nrepository = "Org/a"\nworkflow = "w"\nnotify = ["email"]\nprd = {}\n', "email", id="unknown-channel"
    ),
    pytest.param(
        '[[repo]]\nrepository = "Org/a"\nworkflow = "w"\nnotify = ["telegram"]\nprd = {}\n',
        "telegram-chat",
        id="telegram-without-chat",
    ),
    pytest.param(
        '[[repo]]\nrepository = "Org/a"\nworkflow = "w"\nnotify = "github"\nprd = {}\n', "list", id="notify-str"
    ),
    pytest.param('[[repo]]\nrepository = "Org/a"\nworkflow = "w"\nprd = { ap = "x" }\n', "ap", id="unknown-env-key"),
    pytest.param('[[repo]]\nrepository = "Org/a"\nworkflow = 1\nprd = {}\n', "string", id="non-string"),
    pytest.param('[setings]\nstate-file = "x"\n', "setings", id="unknown-section"),
    pytest.param('[settings]\nstate-file = "a"\nstate_file = "b"\n', "both", id="both-spellings"),
    pytest.param(
        '[[repo]]\nrepository = "Org/a"\nname = "x"\nworkflow = "w"\nprd = {}\n'
        '[[repo]]\nrepository = "Org/b"\nname = "x"\nworkflow = "w"\nprd = {}\n',
        "x-prd",
        id="duplicated-app",
    ),
]


@pytest.mark.parametrize("text, message", INVALID_CONFIGS)
def test_invalid_config_is_rejected(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)


def test_template_is_valid_and_has_no_active_repositories():
    assert parse(CONFIG_TEMPLATE).targets == ()


def test_template_example_repository_is_valid_once_uncommented():
    uncommented = "\n".join(line.removeprefix("# ") for line in CONFIG_TEMPLATE.splitlines())
    assert len(parse(uncommented).targets) == 3


def test_load_config_reports_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="config init"):
        load_config(tmp_path / "nope.toml")


def test_load_config_reports_invalid_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[[repo]\n")
    with pytest.raises(ConfigError, match=str(path)):
        load_config(path)
