import pytest

from dokku_auto_deploy.properties import GLOBAL, Properties
from dokku_auto_deploy.settings import KEYS, ConfigError, check_key, configured_apps, load_app, value_and_origin

WORKFLOW = ".github/workflows/ci.yml"


@pytest.fixture
def properties(tmp_path):
    properties = Properties(tmp_path)
    for key, value in {"repository": "https://github.com/Org/proj", "branch": "develop"}.items():
        properties.set("app", key, value)
    return properties


class TestCheckKey:
    def test_scopes(self):
        assert check_key("app", "branch").name == "branch"
        assert check_key(GLOBAL, "workflow").name == "workflow"
        assert check_key("app", "workflow").name == "workflow"
        assert check_key(GLOBAL, "telegram-bot-token").secret

    @pytest.mark.parametrize(
        "target, key, message",
        [
            pytest.param(GLOBAL, "repository", "per app", id="app-only-globally"),
            pytest.param("app", "telegram-bot-token", "--global", id="global-only-per-app"),
            pytest.param("app", "brnach", "unknown key", id="typo"),
        ],
    )
    def test_invalid(self, target, key, message):
        with pytest.raises(ConfigError, match=message):
            check_key(target, key)


class TestNormalize:
    @pytest.mark.parametrize(
        "key, value, expected",
        [
            pytest.param("repository", "https://github.com/Org/proj.git/", "https://github.com/Org/proj", id="url"),
            pytest.param("notify", "telegram, comment,telegram", "telegram,comment", id="notify-dedup"),
            pytest.param("notify", "none", "none", id="notify-none"),
            pytest.param("telegram-chat", "-1001234567890_42", "-1001234567890_42", id="topic"),
            pytest.param("telegram-chat", "@channel", "@channel", id="channel-name"),
            pytest.param("workflow", "none", "none", id="no-ci"),
            pytest.param("telegram-bot-token", "123456:AAE-x_Y", "123456:AAE-x_Y", id="bot-token"),
        ],
    )
    def test_valid(self, key, value, expected):
        assert KEYS[key].normalize(value) == expected

    @pytest.mark.parametrize(
        "key, value",
        [
            pytest.param("repository", "Org/proj", id="not-url"),
            pytest.param("branch", "feature x", id="branch-space"),
            pytest.param("forge", "bitbucket", id="forge"),
            pytest.param("notify", "email", id="notify-unknown"),
            pytest.param("notify", "comment,", id="notify-empty-item"),
            pytest.param("telegram-chat", "my group", id="chat"),
            pytest.param("workflow", "ci file.yml", id="workflow-space"),
            pytest.param("telegram-bot-token", "123:abc\ndef", id="bot-token-two-lines"),
            pytest.param("telegram-bot-token", "abc", id="bot-token-no-id"),
        ],
    )
    def test_invalid(self, key, value):
        with pytest.raises(ConfigError):
            KEYS[key].normalize(value)


class TestLoadApp:
    def test_app_value_wins_over_global(self, properties):
        properties.set(GLOBAL, "workflow", WORKFLOW)
        properties.set(GLOBAL, "notify", "telegram")
        properties.set(GLOBAL, "telegram-chat", "-100")
        properties.set("app", "telegram-chat", "-200_3")
        config = load_app(properties, "app")
        assert (config.workflow, config.notify, config.telegram_chat) == (WORKFLOW, ("telegram",), "-200_3")
        assert value_and_origin(properties, "app", "workflow") == (WORKFLOW, "global")
        assert value_and_origin(properties, "app", "telegram-chat") == ("-200_3", "app")
        assert value_and_origin(properties, "app", "forge") == (None, "unset")

    def test_workflow_none_overrides_global_workflow(self, properties):
        properties.set(GLOBAL, "workflow", WORKFLOW)
        properties.set("app", "workflow", "none")
        assert load_app(properties, "app").workflow is None

    def test_notify_none_overrides_global_channels(self, properties):
        properties.set(GLOBAL, "notify", "telegram")
        properties.set("app", "workflow", "none")
        properties.set("app", "notify", "none")
        assert load_app(properties, "app").notify == ()

    def test_forge_detected_from_host(self, properties):
        properties.set("app", "workflow", "none")
        config = load_app(properties, "app")
        assert (config.repository.forge, config.repository.url) == ("github", "https://github.com/Org/proj")

    @pytest.mark.parametrize(
        "settings, message",
        [
            pytest.param({}, "workflow is not set", id="workflow"),
            pytest.param({"workflow": "none", "notify": "telegram"}, "telegram-chat is not set", id="chat"),
            pytest.param(
                {"workflow": "none", "repository": "https://git.example.com/Org/proj"}, "forge is not set", id="forge"
            ),
        ],
    )
    def test_missing_settings_say_how_to_set_them(self, properties, settings, message):
        for key, value in settings.items():
            properties.set("app", key, value)
        with pytest.raises(ConfigError, match=message) as exc:
            load_app(properties, "app")
        assert "dokku auto-deploy:set" in str(exc.value)

    def test_github_workflow_must_be_a_workflow_file(self, properties):
        properties.set("app", "workflow", "ci.yml")
        with pytest.raises(ConfigError, match=r"\.github/workflows/"):
            load_app(properties, "app")

    def test_missing_branch(self, properties):
        properties.delete("app", "branch")
        with pytest.raises(ConfigError, match="branch is not set"):
            load_app(properties, "app")


def test_configured_apps_have_a_repository(properties):
    properties.set("other", "branch", "main")
    properties.set(GLOBAL, "workflow", "none")
    assert configured_apps(properties) == ["app"]
