"""Deploy result messages and the channels that deliver them (GitHub PR comments, Telegram)."""

import dataclasses
import html
import logging
import re
import socket
from collections.abc import Iterable
from typing import Any

from dokku_auto_deploy.config import Target
from dokku_auto_deploy.github import DEFAULT_API as GITHUB_API
from dokku_auto_deploy.github import GitHub
from dokku_auto_deploy.telegram import DEFAULT_API as TELEGRAM_API
from dokku_auto_deploy.telegram import Telegram

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclasses.dataclass
class DeployResult:
    target: Target
    sha: str
    success: bool
    output: str
    prs: list[dict[str, Any]]
    app_url: str | None = None


def error_tail(output: str, max_lines: int = 60) -> str:
    """Last lines of the build output, without ANSI colors and safe inside a Markdown code block."""
    lines = ANSI_ESCAPE.sub("", output).rstrip("\n").splitlines()[-max_lines:]
    return "\n".join(lines).replace("```", "'''")


def commit_url(result: DeployResult) -> str:
    return f"https://github.com/{result.target.repository}/commit/{result.sha}"


def comment_body(result: DeployResult) -> str:
    """Markdown comment. The commit is an explicit link: GitHub doesn't autolink a SHA inside backticks."""
    app = result.target.app
    commit = f"[`{result.sha[:8]}`]({commit_url(result)})"
    app_line = f"\n\nApp: [{result.app_url}]({result.app_url})" if result.app_url else ""
    if result.success:
        return f"Deploy of {commit} to `{app}` succeeded.{app_line}"
    return (
        f"Deploy of {commit} to `{app}` **failed**.{app_line}\n\nEnd of the build log:\n\n"
        f"```\n{error_tail(result.output)}\n```\n\n"
        f"Full log on the server: `journalctl -u dokku-auto-deploy`. "
        f"To retry: `dokku-auto-deploy poll --force {app}`."
    )


def telegram_text(result: DeployResult) -> str:
    """Message for `parse_mode=HTML`; the build log is cut (from the start) to fit the length limit.

    The commit link sits behind the word "commit" and each PR link spans "#number title"; the app URL is shown in full.
    """
    target, sha = result.target, result.sha
    status = "succeeded" if result.success else "FAILED"
    lines = [
        f"Deploy {status}: <b>{html.escape(target.app)}</b>",
        f'{html.escape(target.repository)} {html.escape(target.branch)}, <a href="{html.escape(commit_url(result))}">'
        f"commit</a> <code>{sha[:8]}</code>",
    ]
    if result.app_url:
        url = html.escape(result.app_url)
        lines.append(f'<a href="{url}">{url}</a>')
    for pull in result.prs:
        label = html.escape(f"#{pull['number']} {pull.get('title') or ''}".strip())
        lines.append(f'<a href="{html.escape(pull["html_url"])}">{label}</a>')
    text = "\n".join(lines)
    if not result.success:
        room = TELEGRAM_MAX_LENGTH - len(text) - len("\n<pre></pre>")
        log = html.escape(error_tail(result.output, max_lines=30))
        if len(log) > room:
            log = log[-room:]
            if "\n" in log:  # Start at a line boundary, which also avoids starting inside an HTML entity
                log = log[log.index("\n") + 1 :]
        text += f"\n<pre>{log}</pre>"
    return text


def notify(
    result: DeployResult,
    github_token: str,
    telegram_token: str | None,
    github_api: str = GITHUB_API,
    telegram_api: str = TELEGRAM_API,
) -> list[str]:
    """Deliver `result` to every channel enabled for its target; returns one message per failed channel.

    A failing channel never prevents the others: notifications are best-effort and must not turn a finished deploy
    into an error.
    """
    app = result.target.app
    failures = []
    for channel in result.target.notify:
        try:
            if channel == "github":
                if not result.prs:
                    logger.info("[%s] GitHub: no merged pull request in this deploy, nothing to comment", app)
                github = GitHub(github_token, github_api)
                for pull in result.prs:
                    github.comment(result.target.repository, pull["number"], comment_body(result))
                    logger.info("[%s] GitHub: commented on PR #%s", app, pull["number"])
            elif channel == "telegram":
                if not telegram_token:
                    raise RuntimeError("no Telegram bot token (see telegram-token-file in [settings])")
                telegram = Telegram(telegram_token, telegram_api)
                telegram.send_message(result.target.telegram_chat, telegram_text(result))
                logger.info("[%s] Telegram: message sent to %s", app, result.target.telegram_chat)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            failures.append(f"{channel}: {exc}")
    return failures


def telegram_chats(targets: Iterable[Target]) -> dict[str, list[str]]:
    """Apps reported to each Telegram chat, in config order (only targets with the telegram channel)."""
    chats: dict[str, list[str]] = {}
    for target in targets:
        if "telegram" in target.notify:
            chats.setdefault(target.telegram_chat, []).append(target.app)
    return chats


def send_test_messages(
    targets: Iterable[Target], telegram_token: str, telegram_api: str = TELEGRAM_API
) -> list[tuple[str, list[str], str | None]]:
    """Send one test message to each configured Telegram chat; returns (chat, apps, error or None) per chat."""
    telegram = Telegram(telegram_token, telegram_api)
    host = html.escape(socket.gethostname())
    results: list[tuple[str, list[str], str | None]] = []
    for chat, apps in telegram_chats(targets).items():
        names = ", ".join(f"<code>{html.escape(app)}</code>" for app in apps)
        text = f"dokku-auto-deploy test from <b>{host}</b>: deploys of {names} will be reported here."
        try:
            telegram.send_message(chat, text)
            results.append((chat, apps, None))
        except (RuntimeError, OSError) as exc:
            results.append((chat, apps, str(exc)))
    return results
