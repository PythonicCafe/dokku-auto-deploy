"""One polling cycle: for each configured app, deploy its branch head once CI passed, then report the result.

Decision per app (see `decide`):
- head SHA already handled (deployed or deploy failed) -> skip. A failed deploy is not retried on its own, to avoid
  rebuilding a broken commit every cycle; `redeploy` retries it. A head whose CI failed keeps being checked, so a
  successful re-run of its CI deploys it.
- CI of that SHA (push event on that branch, configured workflow) not finished -> wait for the next cycle.
  With `workflow none` there is no CI to wait for.
- CI failed -> recorded, nothing deployed, nobody notified (the forge already shows the red CI).
- CI passed -> `dokku git:sync --build` of that exact SHA, unless the app is locked (manual deploy or `apps:lock`:
  wait), or Dokku already runs it (its last successful deploy): then it is only recorded, so a first run or a lost state doesn't rebuild
  apps that are up to date. `redeploy` rebuilds anyway.
"""

import datetime
import fcntl
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dokku_auto_deploy import dokku
from dokku_auto_deploy.forge import Change, CIStatus, Forge, ForgeError
from dokku_auto_deploy.forges import make_forge
from dokku_auto_deploy.notify import DeployResult, notify
from dokku_auto_deploy.properties import GLOBAL, Properties, dokku_root
from dokku_auto_deploy.repository import RepositoryError, netrc_password
from dokku_auto_deploy.settings import AppConfig, ConfigError, configured_apps, load_app
from dokku_auto_deploy.telegram import DEFAULT_API as TELEGRAM_API

logger = logging.getLogger(__name__)

Action = Literal["skip", "wait", "ci_failed", "deploy"]
OutputCallback = Callable[[bytes], object]
STATE_KEY = "state"


@dataclass
class PollResult:
    errors: int = 0  # Apps that couldn't be handled (invalid config, API errors etc.)
    failed_deploys: int = 0


class PollRunning(RuntimeError):
    pass


def decide(head_sha: str, state: dict[str, Any] | None, ci: CIStatus | None) -> Action:
    """What to do with `head_sha`, given the status of its CI (`None`: the repository has no CI to wait for).

    A head whose CI failed is still watched: a successful re-run of the same commit deploys it.
    """
    if state is not None and state.get("sha") == head_sha and (state.get("status") != "ci_failed" or ci == "failure"):
        return "skip"
    if ci is None or ci == "success":
        return "deploy"
    return "ci_failed" if ci == "failure" else "wait"


def select_merged(changes: list[Change], shas: set[str]) -> list[Change]:
    """Changes with a commit among `shas` (the deployed range)."""
    return [change for change in changes if change.shas & shas]


def merged_changes(forge: Forge, config: AppConfig, previous_sha: str | None, sha: str) -> list[Change]:
    """Changes merged into the app's branch after `previous_sha` up to `sha`.

    Several can land in one deploy (merged while CI was running), so the whole range counts. Without a previous
    deploy, or when the range can't be compared (history rewritten), only `sha` itself is considered.
    """
    shas = {sha}
    if previous_sha and previous_sha != sha:
        try:
            shas |= forge.commits_between(previous_sha, sha)
        except ForgeError as exc:
            logger.warning("[%s] compare %s...%s failed (%s), using head only", config.app, previous_sha, sha, exc)
    return select_merged(forge.merged_changes(config.branch), shas)


def load_state(properties: Properties, app: str) -> dict[str, Any] | None:
    """The app's state, or None if there is none or it can't be read: starting over is safe, since apps already
    running the branch head are only recorded, not rebuilt."""
    value = properties.get(app, STATE_KEY)
    if value is None:
        return None
    try:
        state = json.loads(value)
    except ValueError:
        state = None
    if not isinstance(state, dict):
        logger.warning("[%s] unreadable state, starting over: %r", app, value[:200])
        return None
    return state


def save_state(properties: Properties, app: str, state: dict[str, Any]) -> None:
    properties.set(app, STATE_KEY, json.dumps(state, default=str) + "\n")


def forge_for(config: AppConfig) -> Forge:
    """Forge client authenticated with the token `dokku git:auth <host>` stored for the repository host."""
    netrc_path = dokku_root() / ".netrc"
    try:
        token = netrc_password(netrc_path, config.repository.host)
    except RepositoryError as exc:
        raise ConfigError(str(exc)) from None
    if token is None:
        raise ConfigError(
            f"no token for {config.repository.host} in {netrc_path} "
            f"(cat token-file | dokku git:auth {config.repository.host} <username>)"
        )
    return make_forge(config.repository, token)


def record(properties: Properties, app: str, sha: str, status: str, deployed_sha: str | None) -> None:
    state = {
        "sha": sha,
        "status": status,
        "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "deployed_sha": deployed_sha,
    }
    save_state(properties, app, state)


def notify_deploy(
    config: AppConfig,
    forge: Forge,
    sha: str,
    success: bool,
    output: str,
    previous_deployed: str | None,
    telegram_token: str | None,
    telegram_api: str,
) -> None:
    app = config.app
    changes: list[Change] = []
    if sha != previous_deployed:
        try:
            changes = merged_changes(forge, config, previous_deployed, sha)
        except (ForgeError, KeyError, ValueError) as exc:
            logger.warning("[%s] could not list merged changes: %s", app, exc)
    result = DeployResult(config, sha, success, output, changes, forge.commit_url(sha), dokku.app_url(app))
    for failure in notify(result, forge, telegram_token, telegram_api):
        logger.warning("[%s] notification failed: %s", app, failure)


def process_app(
    config: AppConfig,
    forge: Forge,
    properties: Properties,
    telegram_token: str | None,
    telegram_api: str,
    redeploy: bool,
    on_output: OutputCallback | None,
) -> bool:
    """Handle one app; returns False only when a deploy was attempted and failed."""
    app, branch = config.app, config.branch
    sha = forge.branch_head(branch)
    previous = load_state(properties, app)
    last_deployed = (previous or {}).get("deployed_sha")
    current = None if redeploy else previous
    ci = None
    handled = current is not None and current.get("sha") == sha and current.get("status") != "ci_failed"
    if config.workflow and not handled:
        ci = forge.ci_status(sha, branch, config.workflow)
    action = decide(sha, current, ci)
    if action == "skip":
        return True
    if action == "wait":
        if redeploy:
            logger.info("[%s] %s@%s: redeploy requested, but its CI hasn't passed yet: waiting", app, branch, sha[:8])
        else:
            logger.debug("[%s] %s@%s: waiting for CI", app, branch, sha[:8])
        return True
    if action == "ci_failed":
        logger.info("[%s] %s@%s: CI failed, not deploying", app, branch, sha[:8])
        record(properties, app, sha, "ci_failed", last_deployed)
        return True
    lock = dokku.lock_state(app)
    if lock in ("manual", "deploying"):
        logger.info("[%s] %s@%s: app locked (deploy in progress or apps:lock), waiting", app, branch, sha[:8])
        return True
    if lock == "orphan":
        logger.warning(
            "[%s] deploy lock left behind by a failed deploy, deploying anyway (remove it with: dokku apps:unlock %s)",
            app,
            app,
        )
    if not redeploy and dokku.runs_commit(app, sha):
        logger.info("[%s] %s@%s: app already runs this commit, recorded without rebuilding", app, branch, sha[:8])
        record(properties, app, sha, "deployed", sha)
        return True
    logger.info("[%s] deploying %s@%s", app, branch, sha[:8])
    success, output, timed_out = dokku.git_sync(app, config.repository.clone_url, sha, on_output)
    if timed_out:
        # Not a lost race: our own killed deploy left the lock file behind, so this must be reported, not retried
        logger.error("[%s] deploy timed out; the app may stay locked (dokku apps:unlock %s)", app, app)
    elif not success and dokku.is_locked(app):
        logger.info("[%s] app got locked during our deploy attempt, will retry", app)
        return True
    status = "deployed" if success else "deploy_failed"
    logger.info("[%s] %s: %s", app, sha[:8], status)
    # Recorded before notifying: whatever happens while notifying, this deploy must not run again
    record(properties, app, sha, status, sha if success else last_deployed)
    if config.notify:
        try:
            notify_deploy(config, forge, sha, success, output, last_deployed, telegram_token, telegram_api)
        except Exception as exc:
            logger.warning("[%s] notification failed: %s", app, exc)
    return success


@contextmanager
def poll_lock(path: Path) -> Iterator[None]:
    """Only one cycle at a time (a scheduled one and a manual `poll` may overlap); raises `PollRunning` otherwise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PollRunning("another auto-deploy:poll is running") from None
        yield


def poll(
    properties: Properties,
    redeploy: Iterable[str] = (),
    telegram_api: str = TELEGRAM_API,
    on_output: OutputCallback | None = None,
) -> PollResult:
    """Run one cycle over every configured app; returns how many apps couldn't be handled and how many deploys failed.

    Apps are independent: an error in one is logged and the others still run. Each app's state is saved as soon as
    it is handled, so an interruption keeps what was already done.
    """
    redeploy_apps = set(redeploy)
    telegram_token = properties.get(GLOBAL, "telegram-bot-token")
    result = PollResult()
    for app in configured_apps(properties):
        try:
            config = load_app(properties, app)
            deploy_ok = process_app(
                config, forge_for(config), properties, telegram_token, telegram_api, app in redeploy_apps, on_output
            )
        except Exception as exc:
            result.errors += 1
            logger.error("[%s] %s", app, exc)
        else:
            result.failed_deploys += not deploy_ok
    return result
