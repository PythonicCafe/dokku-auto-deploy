"""Config file (TOML): loading, validation and the commented template written by `config init`.

Every key may be written in kebab-case (`state-file`, as documented) or snake_case, but not both at once. A key that
is absent and a key set to an empty string mean the same thing: "use the default". Unknown sections and keys are
rejected, so a typo never silently falls back to a default.
"""

import dataclasses
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/dokku-auto-deploy/config.toml")
DEFAULT_BRANCHES = {"stg": "develop", "prd": "main"}
CHANNELS = ("github", "telegram")

SETTINGS_KEYS = {
    "state_file": "/var/lib/dokku-auto-deploy/state.json",
    "github_token_file": "/etc/dokku-auto-deploy/github-token",
    "telegram_token_file": "/etc/dokku-auto-deploy/telegram-token",
}
DEFAULTS_KEYS = ("workflow", "telegram_chat")
REPO_KEYS = ("repository", "name", "workflow", "notify", "telegram_chat", *DEFAULT_BRANCHES)
ENVIRONMENT_KEYS = ("app", "branch")


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Settings:
    state_file: Path
    github_token_file: Path
    telegram_token_file: Path


@dataclasses.dataclass(frozen=True)
class Target:
    """One environment of one repository: pushes to `branch` are deployed to the Dokku app `app`."""

    repository: str
    environment: str
    app: str
    branch: str
    workflow: str
    notify: tuple[str, ...]
    telegram_chat: str


@dataclasses.dataclass(frozen=True)
class Config:
    settings: Settings
    targets: tuple[Target, ...]


def _normalize(table: Any, allowed: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    result: dict[str, Any] = {}
    for key, value in table.items():
        name = key.replace("-", "_")
        if name not in allowed:
            valid = ", ".join(item.replace("_", "-") for item in allowed)
            raise ConfigError(f"{where}: unknown key {key!r} (valid: {valid})")
        if name in result:
            raise ConfigError(f"{where}: key {key!r} given in both kebab-case and snake_case")
        result[name] = value
    return result


def _string(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key.replace('_', '-')} must be a string")
    return value.strip()


def _notify(table: dict[str, Any], where: str) -> tuple[str, ...]:
    value = table.get("notify", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f'{where}: notify must be a list of strings, e.g. ["github", "telegram"]')
    channels = tuple(item.strip() for item in value if item.strip())
    unknown = [channel for channel in channels if channel not in CHANNELS]
    if unknown:
        raise ConfigError(f"{where}: unknown notify channel(s) {unknown} (valid: {', '.join(CHANNELS)})")
    return tuple(dict.fromkeys(channels))


def _repo_targets(entry: Any, index: int, defaults: dict[str, Any]) -> list[Target]:
    where = f"[[repo]] #{index}"
    table = _normalize(entry, REPO_KEYS, where)
    repository = _string(table, "repository", where)
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name:
        raise ConfigError(f"{where}: repository must be owner/name, got {repository!r}")
    where = f"[[repo]] {repository}"
    workflow = _string(table, "workflow", where) or _string(defaults, "workflow", "[defaults]")
    if not workflow:
        raise ConfigError(f"{where}: missing workflow (set it here or in [defaults])")
    notify = _notify(table, where)
    telegram_chat = _string(table, "telegram_chat", where) or _string(defaults, "telegram_chat", "[defaults]")
    if "telegram" in notify and not telegram_chat:
        raise ConfigError(f"{where}: notify has telegram but no telegram-chat (set it here or in [defaults])")
    base_name = _string(table, "name", where) or name
    targets = []
    for environment, default_branch in DEFAULT_BRANCHES.items():
        if environment not in table:
            continue
        env_where = f"{where} {environment}"
        options = _normalize(table[environment], ENVIRONMENT_KEYS, env_where)
        targets.append(
            Target(
                repository=repository,
                environment=environment,
                app=_string(options, "app", env_where) or f"{base_name}-{environment}",
                branch=_string(options, "branch", env_where) or default_branch,
                workflow=workflow,
                notify=notify,
                telegram_chat=telegram_chat,
            )
        )
    if not targets:
        environments = " and/or ".join(f"{environment} = {{}}" for environment in DEFAULT_BRANCHES)
        raise ConfigError(f"{where}: no environment configured (add {environments})")
    return targets


def parse_config(raw: dict[str, Any]) -> Config:
    """Build a validated `Config` from the parsed TOML; raises `ConfigError` on any problem."""
    for section in raw:
        if section not in ("settings", "defaults", "repo"):
            raise ConfigError(f"unknown section [{section}] (valid: [settings], [defaults], [[repo]])")
    settings_table = _normalize(raw.get("settings", {}), tuple(SETTINGS_KEYS), "[settings]")
    settings = Settings(
        **{key: Path(_string(settings_table, key, "[settings]") or default) for key, default in SETTINGS_KEYS.items()}
    )
    defaults = _normalize(raw.get("defaults", {}), DEFAULTS_KEYS, "[defaults]")
    repos = raw.get("repo", [])
    if not isinstance(repos, list):
        raise ConfigError("repo must be an array of tables: use [[repo]]")
    targets: list[Target] = []
    for index, entry in enumerate(repos, start=1):
        targets.extend(_repo_targets(entry, index, defaults))
    apps = [target.app for target in targets]
    duplicated = sorted({app for app in apps if apps.count(app) > 1})
    if duplicated:
        raise ConfigError(f"app(s) used by more than one target: {', '.join(duplicated)}")
    return Config(settings=settings, targets=tuple(targets))


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as fobj:
            raw = tomllib.load(fobj)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (create it with `dokku-auto-deploy config init`)") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return parse_config(raw)


CONFIG_TEMPLATE = """\
## dokku-auto-deploy configuration. Keys accept kebab-case or snake_case; a missing key and an empty string both mean
## "use the default". Unknown sections/keys are rejected. `dokku-auto-deploy config show` prints the resolved targets.

[settings]
## Files holding secrets (never put the tokens themselves here). Defaults:
# state-file = "/var/lib/dokku-auto-deploy/state.json"
# github-token-file = "/etc/dokku-auto-deploy/github-token"
# telegram-token-file = "/etc/dokku-auto-deploy/telegram-token"

[defaults]
## Used by every [[repo]] that leaves the same key empty or missing.
# workflow = ".github/workflows/django.yml"  # CI workflow file whose run must succeed before deploying
# telegram-chat = "-1001234567890_42"        # Group id, or group id + "_" + topic id

## One [[repo]] per GitHub repository. Each environment table present (stg, prd) becomes a deploy target:
##   stg: branch "develop" -> app "<name>-stg"
##   prd: branch "main"    -> app "<name>-prd"
## `name` defaults to the repository name; `app` and `branch` can be set per environment.
## notify: channels that receive the result of each deploy: [] (none, default), "github" (comment on the merged pull
## requests) and/or "telegram" (message to telegram-chat).
#
# [[repo]]
# repository = "PythonicCafe/myproject"
# notify = ["github", "telegram"]
# stg = {}
# prd = {}
#
# [[repo]]
# repository = "PythonicCafe/website"
# name = "site"
# workflow = ".github/workflows/ci.yml"
# notify = ["telegram"]
# telegram-chat = "-1009876543210"
# prd = { app = "site-production", branch = "main" }
"""
