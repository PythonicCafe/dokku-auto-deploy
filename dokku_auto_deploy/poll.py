"""One polling cycle: for each target, deploy the branch head once its CI passed, then report the result.

Decision per target (see `decide`):
- head SHA already handled (deployed, CI failed or deploy failed) -> skip. A failed deploy is not retried on its own,
  to avoid rebuilding a broken commit every cycle; `force_apps` retries it.
- CI of that SHA (push event on that branch, configured workflow file) not finished -> wait for the next cycle.
- CI failed -> recorded, nothing deployed, nobody notified (GitHub already shows the red CI).
- CI passed -> `dokku git:sync --build` of that exact SHA, unless the app is locked (manual deploy or `apps:lock`).
"""

import datetime
import json
import logging
import urllib.error
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from dokku_auto_deploy import dokku
from dokku_auto_deploy.config import Config, Target
from dokku_auto_deploy.github import DEFAULT_API as GITHUB_API
from dokku_auto_deploy.github import GitHub
from dokku_auto_deploy.notify import TELEGRAM_API, DeployResult, notify

logger = logging.getLogger(__name__)

Action = Literal["skip", "wait", "ci_failed", "deploy"]
OutputCallback = Callable[[bytes], object]


def decide(head_sha: str, runs: list[dict[str, Any]], state: dict[str, Any] | None, workflow: str) -> Action:
    """What to do with `head_sha`. Only the latest run of `workflow` counts, so a successful re-run wins."""
    if state is not None and state.get("sha") == head_sha:
        return "skip"
    matching = [run for run in runs if run.get("path") == workflow]
    if not matching:
        return "wait"
    latest = max(matching, key=lambda run: int(run["id"]))
    if latest["status"] != "completed":
        return "wait"
    return "deploy" if latest["conclusion"] == "success" else "ci_failed"


def select_merged_prs(pulls: list[dict[str, Any]], shas: set[str]) -> list[dict[str, Any]]:
    """PRs whose merge commit is among `shas` (covers merge, squash and rebase merges)."""
    return [pull for pull in pulls if pull.get("merged_at") and pull.get("merge_commit_sha") in shas]


def merged_prs(github: GitHub, target: Target, previous_sha: str | None, sha: str) -> list[dict[str, Any]]:
    """PRs merged into the target branch after `previous_sha` up to `sha`.

    Several PRs can land in one deploy (merged while CI was running), so the whole range counts. Without a previous
    deploy, or when the range can't be compared (history rewritten), only `sha` itself is considered.
    """
    shas = {sha}
    if previous_sha and previous_sha != sha:
        try:
            shas |= github.commits_between(target.repository, previous_sha, sha)
        except urllib.error.HTTPError as exc:
            logger.warning("[%s] compare %s...%s failed (%s), using head only", target.app, previous_sha, sha, exc)
    return select_merged_prs(github.closed_pulls(target.repository, target.branch), shas)


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
    github: GitHub,
    telegram_token: str | None,
    telegram_api: str,
    force: bool,
    on_output: OutputCallback | None,
) -> None:
    app, branch = target.app, target.branch
    sha = github.branch_head(target.repository, branch)
    last_deployed = state.get(app, {}).get("deployed_sha")
    current = None if force else state.get(app)
    action = decide(sha, github.push_runs(target.repository, branch, sha), current, target.workflow)
    if action == "skip":
        return
    if action == "wait":
        logger.debug("[%s] %s@%s: waiting for CI", app, branch, sha[:8])
        return
    if action == "ci_failed":
        logger.info("[%s] %s@%s: CI failed, not deploying", app, branch, sha[:8])
        status = "ci_failed"
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
            prs: list[dict[str, Any]] = []
            if sha != last_deployed:
                try:
                    prs = merged_prs(github, target, last_deployed, sha)
                except (OSError, KeyError, ValueError) as exc:
                    logger.warning("[%s] could not list merged PRs: %s", app, exc)
            result = DeployResult(target, sha, success, output, prs)
            for failure in notify(result, github.token, telegram_token, github.api, telegram_api):
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
    github = GitHub(github_token, github_api)
    forced = set(force_apps)
    state = load_state(config.settings.state_file)
    errors = 0
    for target in config.targets:
        try:
            process_target(target, state, github, telegram_token, telegram_api, target.app in forced, on_output)
        except Exception as exc:
            errors += 1
            logger.error("[%s] %s", target.app, exc)
        finally:
            save_state(config.settings.state_file, state)
    return errors
