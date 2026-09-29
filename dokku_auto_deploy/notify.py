"""Deploy result messages and the channels that deliver them (GitHub PR comments, Telegram)."""

import dataclasses
import html
import logging
import re
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


def error_tail(output: str, max_lines: int = 60) -> str:
    """Last lines of the build output, without ANSI colors and safe inside a Markdown code block."""
    lines = ANSI_ESCAPE.sub("", output).rstrip("\n").splitlines()[-max_lines:]
    return "\n".join(lines).replace("```", "'''")


def comment_body(result: DeployResult) -> str:
    app, sha = result.target.app, result.sha[:8]
    if result.success:
        return f"Deploy of `{sha}` to `{app}` succeeded."
    return (
        f"Deploy of `{sha}` to `{app}` **failed**. End of the build log:\n\n"
        f"```\n{error_tail(result.output)}\n```\n\n"
        f"Full log on the server: `journalctl -u dokku-auto-deploy`. "
        f"To retry: `dokku-auto-deploy poll --force {app}`."
    )


def telegram_text(result: DeployResult) -> str:
    """Message for `parse_mode=HTML`: URLs stay behind words, and the log is cut (from the start) to fit the limit."""
    target, sha = result.target, result.sha
    status = "succeeded" if result.success else "FAILED"
    commit_url = html.escape(f"https://github.com/{target.repository}/commit/{sha}")
    lines = [
        f"Deploy {status}: <b>{html.escape(target.app)}</b>",
        f'{html.escape(target.repository)} {html.escape(target.branch)}, <a href="{commit_url}">commit</a> '
        f"<code>{sha[:8]}</code>",
    ]
    for pull in result.prs:
        link = f'<a href="{html.escape(pull["html_url"])}">#{pull["number"]}</a>'
        lines.append(f"{link} {html.escape(pull.get('title') or '')}")
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
