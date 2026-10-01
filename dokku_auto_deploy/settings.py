"""Settings: which keys exist, where they can be set (app, global or both), how they're validated and resolved.

A key that can be set in both places takes the app's value when it has one, else the global one. Values are checked
when they're set, and the whole app again when it's resolved, so an invalid combination (e.g. `telegram` in `notify`
without a `telegram-chat`) is reported before any deploy.
"""

import dataclasses
import re
from collections.abc import Callable
from typing import Literal

from dokku_auto_deploy.properties import GLOBAL, Properties
from dokku_auto_deploy.repository import FORGES, Repository, RepositoryError, detect_forge, normalize_url

CHANNELS = ("comment", "telegram")
NONE = "none"
TELEGRAM_CHAT = re.compile(r"-?\d+(_\d+)?|@\w+")
TELEGRAM_BOT_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]+")
GITHUB_WORKFLOWS = ".github/workflows/"
Scope = Literal["app", "global", "both"]


class ConfigError(ValueError):
    pass


def _repository(value: str) -> str:
    try:
        return normalize_url(value)
    except RepositoryError as exc:
        raise ConfigError(str(exc)) from None


def _branch(value: str) -> str:
    if not value or any(char.isspace() for char in value) or value.startswith("-"):
        raise ConfigError(f"invalid branch name: {value!r}")
    return value


def _forge(value: str) -> str:
    if value not in FORGES:
        raise ConfigError(f"unknown forge {value!r} (expected one of: {', '.join(FORGES)})")
    return value


def _workflow(value: str) -> str:
    if any(char.isspace() for char in value):
        raise ConfigError(f"invalid workflow {value!r}: expected a file path like .github/workflows/ci.yml, or none")
    return value


def _notify(value: str) -> str:
    if value == NONE:
        return value
    channels = [channel.strip() for channel in value.split(",")]
    unknown = [channel for channel in channels if channel not in CHANNELS]
    if unknown or not all(channels):
        raise ConfigError(
            f"invalid notify {value!r}: expected a comma-separated list of {', '.join(CHANNELS)}, or none"
        )
    return ",".join(dict.fromkeys(channels))


def _telegram_chat(value: str) -> str:
    if not TELEGRAM_CHAT.fullmatch(value):
        raise ConfigError(f"invalid telegram-chat {value!r}: expected a chat id (-100123), with a topic (-100123_45)")
    return value


def _telegram_bot_token(value: str) -> str:
    if not TELEGRAM_BOT_TOKEN.fullmatch(value):  # The value is a secret: never echo it
        raise ConfigError("invalid telegram-bot-token: expected <bot id>:<secret>, as given by @BotFather")
    return value


@dataclasses.dataclass(frozen=True)
class Key:
    name: str
    scope: Scope
    description: str
    normalize: Callable[[str], str]
    secret: bool = False


KEYS = {
    key.name: key
    for key in (
        Key("repository", "app", "Repository web URL; setting it enables auto-deploy for the app", _repository),
        Key("branch", "app", "Branch to deploy", _branch),
        Key("forge", "app", f"Forge type, only needed for unknown hosts: {', '.join(FORGES)}", _forge),
        Key(
            "workflow",
            "both",
            "CI workflow file to wait for (GitLab: any), or none to deploy without waiting",
            _workflow,
        ),
        Key("notify", "both", f"Comma-separated channels ({', '.join(CHANNELS)}), or none", _notify),
        Key(
            "telegram-chat", "both", "Telegram chat id, optionally with a topic: -100123 or -100123_45", _telegram_chat
        ),
        Key("telegram-bot-token", "global", "Telegram bot token, read from stdin", _telegram_bot_token, secret=True),
    )
}


def check_key(target: str, key: str) -> Key:
    """The `Key` named `key`, if it can be set on `target` (an app or `--global`)."""
    if key not in KEYS:
        raise ConfigError(f"unknown key {key!r} (valid keys: {', '.join(KEYS)})")
    spec = KEYS[key]
    if target == GLOBAL and spec.scope == "app":
        raise ConfigError(f"{key} can only be set per app")
    if target != GLOBAL and spec.scope == "global":
        raise ConfigError(f"{key} can only be set with --global")
    return spec


@dataclasses.dataclass(frozen=True)
class AppConfig:
    app: str
    repository: Repository
    branch: str
    workflow: str | None  # None: don't wait for CI
    notify: tuple[str, ...]
    telegram_chat: str | None


def value_and_origin(properties: Properties, app: str, key: str) -> tuple[str | None, str]:
    """(value, "app" | "global" | "unset") for `key` of `app`, following the app -> global fallback."""
    value = properties.get(app, key)
    if value is not None:
        return value, "app"
    if KEYS[key].scope == "both":
        value = properties.get(GLOBAL, key)
        if value is not None:
            return value, "global"
    return None, "unset"


def load_app(properties: Properties, app: str) -> AppConfig:
    """Resolved settings of `app`; `ConfigError` says what's missing and how to set it."""

    def get(key: str) -> str | None:
        return value_and_origin(properties, app, key)[0]

    def missing(key: str, example: str) -> ConfigError:
        where = "<app>|--global" if KEYS[key].scope == "both" else app
        return ConfigError(f"{key} is not set (dokku auto-deploy:set {where} {key} {example})")

    url = get("repository")
    if url is None:
        raise missing("repository", "https://github.com/owner/name")
    branch = get("branch")
    if branch is None:
        raise missing("branch", "main")
    forge = get("forge") or detect_forge(url)
    if forge is None:
        raise missing("forge", "|".join(FORGES))
    try:
        repository = Repository(url, forge)
    except RepositoryError as exc:
        raise ConfigError(str(exc)) from None
    workflow = get("workflow")
    if workflow is None:
        raise missing("workflow", ".github/workflows/ci.yml|none")
    if forge == "github" and workflow != NONE and not workflow.startswith(GITHUB_WORKFLOWS):
        # GitHub reports runs by full path: any other value would wait forever
        raise ConfigError(f"workflow {workflow} is not a GitHub workflow file ({GITHUB_WORKFLOWS}<file>.yml)")
    notify_value = get("notify") or NONE
    notify = () if notify_value == NONE else tuple(notify_value.split(","))
    telegram_chat = get("telegram-chat")
    if "telegram" in notify and telegram_chat is None:
        raise missing("telegram-chat", "-100123_45")
    return AppConfig(
        app=app,
        repository=repository,
        branch=branch,
        workflow=None if workflow == NONE else workflow,
        notify=notify,
        telegram_chat=telegram_chat,
    )


def configured_apps(properties: Properties) -> list[str]:
    """Apps with a repository set, i.e. with auto-deploy enabled."""
    return [app for app in properties.apps() if properties.get(app, "repository") is not None]
