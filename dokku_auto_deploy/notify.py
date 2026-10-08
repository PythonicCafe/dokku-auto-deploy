"""Deploy result messages and the channels that deliver them (comments on merged changes, Telegram)."""

import dataclasses
import html
import logging
import re
import socket

from dokku_auto_deploy.forge import Change, Forge, ForgeError
from dokku_auto_deploy.settings import AppConfig
from dokku_auto_deploy.telegram import DEFAULT_API as TELEGRAM_API
from dokku_auto_deploy.telegram import Telegram

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
TELEGRAM_LOG_ROOM = 1500  # Kept for the build log of a failed deploy, however many changes it has
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclasses.dataclass
class DeployResult:
    target: AppConfig
    sha: str
    success: bool
    output: str
    changes: list[Change]
    commit_url: str
    app_url: str | None = None


def error_tail(output: str, max_lines: int = 60) -> str:
    """Last lines of the build output, without ANSI colors and safe inside a Markdown code block."""
    lines = ANSI_ESCAPE.sub("", output).rstrip("\n").splitlines()[-max_lines:]
    return "\n".join(lines).replace("```", "'''")


def comment_body(result: DeployResult) -> str:
    """Markdown comment. The commit is an explicit link: forges don't autolink a SHA inside backticks."""
    app = result.target.app
    commit = f"[`{result.sha[:8]}`]({result.commit_url})"
    app_line = f"\n\nApp: [{result.app_url}]({result.app_url})" if result.app_url else ""
    if result.success:
        return f"Deploy of {commit} to `{app}` succeeded.{app_line}"
    return (
        f"Deploy of {commit} to `{app}` **failed**.{app_line}\n\nEnd of the build log:\n\n"
        f"```\n{error_tail(result.output)}\n```\n\n"
        f"To retry: `dokku auto-deploy:poll --redeploy {app}`."
    )


def _fit_log(log: str, room: int) -> str:
    """HTML-escaped end of `log` that fits in `room` characters, starting at a line boundary when possible.

    Cut before escaping, so the cut never falls inside an entity like `&amp;`.
    """
    kept: list[str] = []
    size = 0
    for line in reversed(log.splitlines()):
        escaped = html.escape(line)
        cost = len(escaped) + (1 if kept else 0)
        if size + cost > room:
            if not kept:  # Not even the last line fits: keep as much of its end as fits
                tail: list[str] = []
                for char in reversed(line):
                    escaped_char = html.escape(char)
                    if size + len(escaped_char) > room:
                        break
                    tail.append(escaped_char)
                    size += len(escaped_char)
                kept.append("".join(reversed(tail)))
            break
        kept.append(escaped)
        size += cost
    return "\n".join(reversed(kept))


def telegram_text(result: DeployResult) -> str:
    """Message for `parse_mode=HTML`, within Telegram's length limit.

    The commit link sits behind the word "commit" and each change link spans its reference and title ("#12 Title");
    the app URL is shown in full. Changes that don't fit become "and N more", always leaving room for the build log of
    a failed deploy, which is cut from its start.
    """
    target, sha = result.target, result.sha
    status = "succeeded" if result.success else "FAILED"
    lines = [
        f"Deploy {status}: <b>{html.escape(target.app)}</b>",
        f'{html.escape(target.repository.path)} {html.escape(target.branch)}, <a href="{html.escape(result.commit_url)}">'
        f"commit</a> <code>{sha[:8]}</code>",
    ]
    if result.app_url:
        url = html.escape(result.app_url)
        lines.append(f'<a href="{url}">{url}</a>')
    reserved = 0 if result.success else TELEGRAM_LOG_ROOM
    more_room = len(f"\nand {len(result.changes)} more")
    room = TELEGRAM_MAX_LENGTH - reserved - more_room - len("\n".join(lines))
    for index, change in enumerate(result.changes):
        label = html.escape(f"{change.reference} {change.title}".strip())
        line = f'<a href="{html.escape(change.url)}">{label}</a>'
        if len(line) + 1 > room:
            lines.append(f"and {len(result.changes) - index} more")
            break
        lines.append(line)
        room -= len(line) + 1
    text = "\n".join(lines)
    if not result.success:
        room = TELEGRAM_MAX_LENGTH - len(text) - len("\n<pre></pre>")
        text += f"\n<pre>{_fit_log(error_tail(result.output, max_lines=30), room)}</pre>"
    return text


def notify(
    result: DeployResult,
    forge: Forge,
    telegram_token: str | None,
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
            if channel == "comment":
                if not result.changes:
                    logger.info("[%s] %s: no merged change in this deploy, nothing to comment", app, forge.name)
                for change in result.changes:
                    forge.comment(change, comment_body(result))
                    logger.info("[%s] %s: commented on %s", app, forge.name, change.reference)
            elif channel == "telegram":
                if not telegram_token:
                    raise RuntimeError(
                        "no Telegram bot token (cat token-file | dokku auto-deploy:set --global telegram-bot-token)"
                    )
                assert result.target.telegram_chat is not None  # Checked by `load_app`
                telegram = Telegram(telegram_token, telegram_api)
                telegram.send_message(result.target.telegram_chat, telegram_text(result))
                logger.info("[%s] Telegram: message sent to %s", app, result.target.telegram_chat)
        except (ForgeError, RuntimeError, OSError, KeyError, ValueError) as exc:
            failures.append(f"{channel}: {exc}")
    return failures


def send_test_message(config: AppConfig, telegram_token: str, telegram_api: str = TELEGRAM_API) -> None:
    """Send a test message to the app's Telegram chat; errors raise `TelegramError`."""
    assert config.telegram_chat is not None
    host = html.escape(socket.gethostname())
    text = f"dokku auto-deploy test from <b>{host}</b>: deploys of <code>{html.escape(config.app)}</code> will be reported here."
    Telegram(telegram_token, telegram_api).send_message(config.telegram_chat, text)


def send_test_comment(config: AppConfig, forge: Forge, number: int) -> None:
    """Comment on change `number` of the app's repository; errors raise `ForgeError`."""
    if "comment" in config.notify:
        scope = f"Deploys of `{config.app}` will be reported on the changes merged into `{config.branch}`."
    else:
        scope = f"`{config.app}` doesn't have `comment` in `notify`, so its deploys won't be commented on."
    body = f"Test comment from dokku auto-deploy on `{socket.gethostname()}`. {scope}"
    forge.comment(Change(number, f"#{number}", "", "", frozenset()), body)
