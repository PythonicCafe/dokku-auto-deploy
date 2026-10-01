"""`auto-deploy:report` data, in Dokku's report conventions (`common.ReportSingleApp` in Dokku's source).

Every value has a flag named `--auto-deploy-<name>`: `report <app> --auto-deploy-<name>` prints only that value, JSON
uses `<name>` as key, and the text output turns it into a label ("Auto deploy computed workflow"). Keys that can be
set per app and globally have three flags, like Dokku's own plugins: `<key>` (the app's value), `global-<key>` and
`computed-<key>` (the one in effect, defaults included).
"""

import json
import urllib.parse
from pathlib import Path

from dokku_auto_deploy.properties import GLOBAL, PLUGIN, Properties, dokku_root
from dokku_auto_deploy.repository import RepositoryError, detect_forge, netrc_login, normalize_url
from dokku_auto_deploy.settings import KEYS, NONE, ConfigError, load_app

FLAG_PREFIX = f"--{PLUGIN}-"
MIN_LABEL_WIDTH = 31  # Same as Dokku


def _global_values(properties: Properties) -> dict[str, str]:
    values = {}
    for key in KEYS.values():
        if key.scope == "app":
            continue
        value = properties.get(GLOBAL, key.name)
        if key.secret:
            values[f"global-{key.name}-set"] = "true" if value else "false"
        else:
            values[f"global-{key.name}"] = value or ""
    return values


def global_report(properties: Properties) -> dict[str, str]:
    from dokku_auto_deploy.schedule import status

    current = status(properties)
    return {**_global_values(properties), "schedule-cron": str(current.cron).lower(), "systemd-timer": current.timer}


def _forge_login(url: str | None, netrc_path: Path) -> str:
    if not url:
        return ""
    try:
        host = urllib.parse.urlsplit(normalize_url(url)).hostname or ""
        return netrc_login(netrc_path, host) or ""
    except RepositoryError:
        return ""


def app_report(properties: Properties, app: str) -> dict[str, str]:
    from dokku_auto_deploy.poll import load_state

    values = {"enabled": "true" if properties.get(app, "repository") else "false"}
    for key in KEYS.values():
        if key.scope == "global":
            continue
        own = properties.get(app, key.name) or ""
        values[key.name] = own
        if key.scope == "both":
            global_value = properties.get(GLOBAL, key.name) or ""
            values[f"global-{key.name}"] = global_value
            default = NONE if key.name in ("workflow", "notify") else ""
            values[f"computed-{key.name}"] = own or global_value or default
    values["computed-forge"] = values["forge"] or detect_forge(values["repository"]) or ""
    values["global-telegram-bot-token-set"] = _global_values(properties)["global-telegram-bot-token-set"]
    values["forge-login"] = _forge_login(values["repository"], dokku_root() / ".netrc")
    try:
        load_app(properties, app)
        values["problem"] = ""
    except ConfigError as exc:
        values["problem"] = str(exc)
    state = load_state(properties, app) or {}
    values["last-sha"] = str(state.get("sha") or "")
    values["last-status"] = str(state.get("status") or "")
    values["last-at"] = str(state.get("at") or "")
    values["deployed-sha"] = str(state.get("deployed_sha") or "")
    return values


def flags(values: dict[str, str]) -> list[str]:
    return sorted(f"{FLAG_PREFIX}{name}" for name in values)


def render_text(title: str, values: dict[str, str]) -> str:
    """Dokku's layout: a `=====>` title, then `Label: value` lines sorted by flag, labels padded to a common width."""
    width = max([MIN_LABEL_WIDTH, *(len(flag) for flag in flags(values))])
    lines = [f"=====> {title}"]
    for flag in flags(values):
        label = flag.removeprefix("--").replace("-", " ").capitalize() + ":"
        lines.append(f"       {label:<{width}}{values[flag.removeprefix(FLAG_PREFIX)]}")
    return "\n".join(lines)


def render_json(values: dict[str, str] | dict[str, dict[str, str]]) -> str:
    return json.dumps(values, sort_keys=True)
