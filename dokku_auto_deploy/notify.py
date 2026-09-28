"""Deploy result messages and the channels that deliver them (GitHub PR comments, Telegram)."""

import dataclasses
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from dokku_auto_deploy.config import Target
from dokku_auto_deploy.github import DEFAULT_API as GITHUB_API
from dokku_auto_deploy.github import GitHub

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT = 10
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


def parse_telegram_chat(chat: str) -> tuple[str, str | None]:
    """`-100123_45` -> ("-100123", "45") for a topic in a group; `-100123` -> ("-100123", None)."""
    chat_id, _, thread_id = chat.partition("_")
    return chat_id, thread_id or None


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


def send_telegram(token: str, chat: str, text: str, api: str = TELEGRAM_API) -> None:
    """Send `text` (HTML) to `chat`; raises `RuntimeError` with Telegram's description, never with the token."""
    chat_id, thread_id = parse_telegram_chat(chat)
    fields = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if thread_id:
        fields["message_thread_id"] = thread_id
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(f"{api.rstrip('/')}/bot{token}/sendMessage", data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT):
            pass
    except urllib.error.HTTPError as exc:
        try:
            description = json.load(exc).get("description", "")
        except ValueError:
            description = ""
        raise RuntimeError(f"Telegram API returned HTTP {exc.code}: {description or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Telegram API: {exc.reason}") from None


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
    failures = []
    for channel in result.target.notify:
        try:
            if channel == "github":
                github = GitHub(github_token, github_api)
                for pull in result.prs:
                    github.comment(result.target.repository, pull["number"], comment_body(result))
            elif channel == "telegram":
                if not telegram_token:
                    raise RuntimeError("no Telegram bot token (see telegram-token-file in [settings])")
                send_telegram(telegram_token, result.target.telegram_chat, telegram_text(result), telegram_api)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            failures.append(f"{channel}: {exc}")
    return failures
