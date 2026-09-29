import json
import os
import stat
import threading
import time
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


class FakeAPI:
    """In-process HTTP server standing in for both GitHub and Telegram APIs.

    `routes` maps "METHOD /path" (no query string) to a (status, JSON body) pair; every request is recorded in
    `requests` as (method, path, query, body) so tests can assert on what was sent. `response_delay` (seconds) makes
    every answer slow, to exercise client timeouts.
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.response_delay = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    body: Any = json.loads(raw)
                else:
                    body = dict(urllib.parse.parse_qsl(raw.decode()))
                api.requests.append((method, parsed.path, query, body))
                time.sleep(api.response_delay)
                status, payload = api.routes.get(f"{method} {parsed.path}", (404, {"message": "Not Found"}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

        return Handler

    def posts(self, prefix: str = "") -> list[tuple[str, Any]]:
        return [(path, body) for method, path, _, body in self.requests if method == "POST" and path.startswith(prefix)]


@pytest.fixture
def fake_api() -> Iterator[FakeAPI]:
    api = FakeAPI()
    thread = threading.Thread(target=api.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield api
    api.server.shutdown()
    api.server.server_close()


FAKE_DOKKU = """#!/bin/sh
echo "$@" >> "$FAKE_DOKKU_DIR/calls"
case "$1" in
  apps:locked) [ -f "$FAKE_DOKKU_DIR/locked" ] && exit 0 || exit 1 ;;
  git:sync)
    [ -f "$FAKE_DOKKU_DIR/lock-during-sync" ] && touch "$FAKE_DOKKU_DIR/locked"
    cat "$FAKE_DOKKU_DIR/output" 2>/dev/null
    exit "$(cat "$FAKE_DOKKU_DIR/exit-code" 2>/dev/null || echo 0)" ;;
esac
"""


class FakeDokku:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def set(
        self, *, exit_code: int = 0, output: str = "", locked: bool = False, lock_during_sync: bool = False
    ) -> None:
        (self.directory / "exit-code").write_text(str(exit_code))
        (self.directory / "output").write_text(output)
        for flag, enabled in (("locked", locked), ("lock-during-sync", lock_during_sync)):
            path = self.directory / flag
            if enabled:
                path.touch()
            else:
                path.unlink(missing_ok=True)

    @property
    def calls(self) -> list[str]:
        path = self.directory / "calls"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def syncs(self) -> list[str]:
        return [call for call in self.calls if call.startswith("git:sync")]


@pytest.fixture
def fake_dokku(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDokku:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "dokku"
    script.write_text(FAKE_DOKKU)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    state_dir = tmp_path / "fake-dokku"
    state_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOKKU_DIR", str(state_dir))
    return FakeDokku(state_dir)
