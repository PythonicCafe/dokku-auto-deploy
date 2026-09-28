"""Minimal Telegram Bot API client (stdlib only): sending a message to a group or to a topic of a group."""

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API = "https://api.telegram.org"
HTTP_TIMEOUT = 10


class TelegramError(RuntimeError):
    pass


def parse_chat(chat: str) -> tuple[str, str | None]:
    """`-100123_45` -> ("-100123", "45") for a topic in a group; `-100123` -> ("-100123", None)."""
    chat_id, _, thread_id = chat.partition("_")
    return chat_id, thread_id or None


class Telegram:
    """The bot token is part of every request URL, so neither URLs nor this object's repr are ever shown."""

    def __init__(self, token: str, api: str = DEFAULT_API) -> None:
        self.token = token
        self.api = api.rstrip("/")

    def __repr__(self) -> str:
        return f"Telegram(api={self.api!r})"

    def send_message(self, chat: str, text: str, parse_mode: str = "HTML") -> None:
        """Send `text` to `chat` (see `parse_chat`); errors raise `TelegramError` without the token."""
        chat_id, thread_id = parse_chat(chat)
        fields = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if thread_id:
            fields["message_thread_id"] = thread_id
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(f"{self.api}/bot{self.token}/sendMessage", data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT):
                pass
        except urllib.error.HTTPError as exc:
            try:
                description = json.load(exc).get("description", "")
            except ValueError:
                description = ""
            raise TelegramError(f"Telegram API returned HTTP {exc.code}: {description or exc.reason}") from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"could not reach Telegram API: {exc.reason}") from None
