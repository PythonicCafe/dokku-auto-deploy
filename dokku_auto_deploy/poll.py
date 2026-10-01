"""One polling cycle: for each target, deploy the branch head once its CI passed, then report the result.

Decision per target (see `decide`):
- head SHA already handled (deployed, CI failed or deploy failed) -> skip. A failed deploy is not retried on its own,
  to avoid rebuilding a broken commit every cycle; `force_apps` retries it.
- CI of that SHA (push event on that branch, configured workflow) not finished -> wait for the next cycle.
  With an empty workflow there is no CI to wait for.
- CI failed -> recorded, nothing deployed, nobody notified (the forge already shows the red CI).
- CI passed -> `dokku git:sync --build` of that exact SHA, unless the app is locked (manual deploy or `apps:lock`),
  or Dokku already runs it (its last successful deploy): then it is only recorded, so a first run or a lost state file doesn't rebuild
  apps that are up to date. `force_apps` rebuilds anyway.
"""

import datetime
import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from dokku_auto_deploy import dokku
from dokku_auto_deploy.config import Config, Target
from dokku_auto_deploy.forge import Change, CIStatus, Forge, ForgeError
from dokku_auto_deploy.github import DEFAULT_API as GITHUB_API
from dokku_auto_deploy.github import GitHub
from dokku_auto_deploy.notify import DeployResult, notify
from dokku_auto_deploy.telegram import DEFAULT_API as TELEGRAM_API

logger = logging.getLogger(__name__)

Action = Literal["skip", "wait", "ci_failed", "deploy"]
OutputCallback = Callable[[bytes], object]


def decide(head_sha: str, state: dict[str, Any] | None, ci: CIStatus | None) -> Action:
    """What to do with `head_sha`, given the status of its CI (`None`: the repository has no CI to wait for)."""
    if state is not None and state.get("sha") == head_sha:
        return "skip"
    if ci is None or ci == "success":
        return "deploy"
    return "ci_failed" if ci == "failure" else "wait"


def select_merged(changes: list[Change], shas: set[str]) -> list[Change]:
    """Changes with a commit among `shas` (the deployed range)."""
    return [change for change in changes if change.shas & shas]


def merged_changes(forge: Forge, target: Target, previous_sha: str | None, sha: str) -> list[Change]:
    """Changes merged into the target branch after `previous_sha` up to `sha`.

    Several can land in one deploy (merged while CI was running), so the whole range counts. Without a previous
    deploy, or when the range can't be compared (history rewritten), only `sha` itself is considered.
    """
    shas = {sha}
    if previous_sha and previous_sha != sha:
        try:
            shas |= forge.commits_between(previous_sha, sha)
        except ForgeError as exc:
            logger.warning("[%s] compare %s...%s failed (%s), using head only", target.app, previous_sha, sha, exc)
    return select_merged(forge.merged_changes(target.branch), shas)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    state: dict[str, dict[str, Any]] = json.loads(path.read_text())
    return state


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    """Write atomically (temp file + rename): a crash mid-write must not lose what was already deployed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2, default=str) + "\n")
    temp.replace(path)


def process_target(
    target: Target,
    state: dict[str, dict[str, Any]],
    forge: Forge,
    telegram_token: str | None,
    telegram_api: str,
    force: bool,
    on_output: OutputCallback | None,
) -> None:
    app, branch = target.app, target.branch
    sha = forge.branch_head(branch)
    last_deployed = state.get(app, {}).get("deployed_sha")
    current = None if force else state.get(app)
    ci = None
    if target.workflow and (current is None or current.get("sha") != sha):
        ci = forge.ci_status(sha, branch, target.workflow)
    action = decide(sha, current, ci)
    if action == "skip":
        return
    if action == "wait":
        logger.debug("[%s] %s@%s: waiting for CI", app, branch, sha[:8])
        return
    if action == "ci_failed":
        logger.info("[%s] %s@%s: CI failed, not deploying", app, branch, sha[:8])
        status = "ci_failed"
    elif not force and dokku.runs_commit(app, sha):
        logger.info("[%s] %s@%s: app already runs this commit, recorded without rebuilding", app, branch, sha[:8])
        status = "deployed"
        last_deployed = sha
    else:
        if dokku.is_locked(app):
            logger.info("[%s] %s@%s: app locked (deploy in progress or apps:lock), waiting", app, branch, sha[:8])
            return
        logger.info("[%s] deploying %s@%s", app, branch, sha[:8])
        success, output = dokku.git_sync(app, target.repository, sha, on_output)
        if not success and dokku.is_locked(app):
            logger.info("[%s] app got locked during our deploy attempt, will retry", app)
            return
        status = "deployed" if success else "deploy_failed"
        logger.info("[%s] %s: %s", app, sha[:8], status)
        if target.notify:
            changes: list[Change] = []
            if sha != last_deployed:
                try:
                    changes = merged_changes(forge, target, last_deployed, sha)
                except (ForgeError, KeyError, ValueError) as exc:
                    logger.warning("[%s] could not list merged changes: %s", app, exc)
            result = DeployResult(target, sha, success, output, changes, forge.commit_url(sha), dokku.app_url(app))
            for failure in notify(result, forge, telegram_token, telegram_api):
                logger.warning("[%s] notification failed: %s", app, failure)
        if success:
            last_deployed = sha
    state[app] = {
        "sha": sha,
        "status": status,
        "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "deployed_sha": last_deployed,
    }


def poll(
    config: Config,
    github_token: str,
    telegram_token: str | None,
    force_apps: Iterable[str] = (),
    github_api: str = GITHUB_API,
    telegram_api: str = TELEGRAM_API,
    on_output: OutputCallback | None = None,
) -> int:
    """Run one cycle over every target; returns how many targets failed (API errors, dokku not found etc.).

    Targets are independent: an error in one is logged and the others still run. State is saved after each target so
    an interruption keeps what was already done.
    """
    forced = set(force_apps)
    state = load_state(config.settings.state_file)
    errors = 0
    for target in config.targets:
        try:
            forge = GitHub(target.repository, github_token, github_api)
            process_target(target, state, forge, telegram_token, telegram_api, target.app in forced, on_output)
        except Exception as exc:
            errors += 1
            logger.error("[%s] %s", target.app, exc)
        finally:
            save_state(config.settings.state_file, state)
    return errors
