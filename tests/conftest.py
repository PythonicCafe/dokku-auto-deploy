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

from dokku_auto_deploy.properties import Properties


class FakeAPI:
    """In-process HTTP server standing in for the forge APIs and Telegram.

    `routes` maps "METHOD /path" (no query string) to a (status, JSON body) pair (bytes are sent as they are; status 0
    closes the connection without an answer); every request is recorded in
    `requests` as (method, path, query, body) so tests can assert on what was sent. `response_delay` (seconds) makes
    every answer slow, to exercise client timeouts.
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.headers: list[dict[str, str]] = []
        self.response_delay = 0.0
        self.redirects: dict[str, str] = {}  # "METHOD /path" -> URL answered with a 302
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
                api.headers.append(dict(self.headers))
                time.sleep(api.response_delay)
                if f"{method} {parsed.path}" in api.redirects:
                    self.send_response(302)
                    self.send_header("Location", api.redirects[f"{method} {parsed.path}"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, payload = api.routes.get(f"{method} {parsed.path}", (404, {"message": "Not Found"}))
                if status == 0:  # Drop the connection without answering
                    self.close_connection = True
                    return
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
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


def _serve() -> Iterator[FakeAPI]:
    api = FakeAPI()
    thread = threading.Thread(target=api.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield api
    api.server.shutdown()
    api.server.server_close()


@pytest.fixture
def fake_api() -> Iterator[FakeAPI]:
    yield from _serve()


@pytest.fixture
def other_api() -> Iterator[FakeAPI]:
    """A second server, on another port: another origin for redirect tests."""
    yield from _serve()


FAKE_APP = "proj-stg"  # The app whose lock file `FakeDokku.set(lock=...)` writes

FAKE_DOKKU = """#!/bin/sh
echo "$@" >> "$FAKE_DOKKU_DIR/calls"
case "$1" in
  apps:exists) [ -f "$FAKE_DOKKU_DIR/missing-$2" ] && exit 20 || exit 0 ;;
  builds:list) cat "$FAKE_DOKKU_DIR/builds.json" 2>/dev/null || echo "[]"; exit 0 ;;
  apps:report) cat "$FAKE_DOKKU_DIR/deploy-source" 2>/dev/null; echo; exit 0 ;;
  url) cat "$FAKE_DOKKU_DIR/url" 2>/dev/null; exit 0 ;;
  git:sync)
    [ -f "$FAKE_DOKKU_DIR/lock-during-sync" ] && cp "$FAKE_DOKKU_DIR/lock-during-sync" "$FAKE_LOCK_FILE"
    cat "$FAKE_DOKKU_DIR/output" 2>/dev/null
    exit "$(cat "$FAKE_DOKKU_DIR/exit-code" 2>/dev/null || echo 0)" ;;
esac
"""


class FakeDokku:
    """State of the fake `dokku` command. Locks are real lock files, as Dokku writes them, for the app under test."""

    BUILD_ID = "fakebuild1"

    def __init__(self, directory: Path, lock_file: Path) -> None:
        self.directory = directory
        self.lock_file = lock_file

    def _lock_content(self, lock: str) -> str:
        """ "manual": empty, like `apps:lock`; "deploying"/"orphan": a build id that is running, or not anymore."""
        if lock == "manual":
            return ""
        status = "running" if lock == "deploying" else "abandoned"
        builds = [{"id": self.BUILD_ID, "status": "running", "display_status": status}]
        (self.directory / "builds.json").write_text(json.dumps(builds))
        return self.BUILD_ID + "\n"

    def set(
        self,
        *,
        exit_code: int = 0,
        output: str = "",
        lock: str | None = None,
        lock_during_sync: str | None = None,
        deploy_source: str = "",
        url: str = "",
    ) -> None:
        (self.directory / "url").write_text(url + "\n" if url else "")
        (self.directory / "exit-code").write_text(str(exit_code))
        (self.directory / "output").write_text(output)
        (self.directory / "deploy-source").write_text(deploy_source)
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        if lock is None:
            self.lock_file.unlink(missing_ok=True)
        else:
            self.lock_file.write_text(self._lock_content(lock))
        during_sync = self.directory / "lock-during-sync"
        if lock_during_sync is None:
            during_sync.unlink(missing_ok=True)
        else:
            during_sync.write_text(self._lock_content(lock_during_sync))

    def set_timer(self, state: str | None) -> None:
        """systemd timer state: "enabled", "disabled", or None when not installed."""
        path = self.directory / "timer"
        if state is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(state + "\n")

    def remove_app(self, app: str) -> None:
        (self.directory / f"missing-{app}").touch()

    @property
    def calls(self) -> list[str]:
        path = self.directory / "calls"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def syncs(self) -> list[str]:
        return [call for call in self.calls if call.startswith("git:sync")]


# `plugn trigger ...` is recorded like a dokku call. `scheduler-cron-write` writes the crontab `crontab -l` shows, like
# Dokku: with the task only when the schedule is cron and no `no-host-cron` file says that no app uses docker-local.
# `systemctl is-enabled` answers with the `timer` file's content
FAKE_PLUGN = """#!/bin/sh
echo "plugn $@" >> "$FAKE_DOKKU_DIR/calls"
if [ "$2" = scheduler-cron-write ]; then
  : > "$FAKE_DOKKU_DIR/crontab"
  if [ "$(cat "$DOKKU_LIB_ROOT/config/auto-deploy/--global/schedule" 2>/dev/null)" = cron ] \\
    && [ ! -f "$FAKE_DOKKU_DIR/no-host-cron" ]; then
    echo "* * * * * dokku auto-deploy:poll &>> /var/log/dokku/auto-deploy.log" > "$FAKE_DOKKU_DIR/crontab"
  fi
fi
"""
FAKE_CRONTAB = """#!/bin/sh
[ -s "$FAKE_DOKKU_DIR/crontab" ] || { echo "no crontab for dokku" >&2; exit 1; }
cat "$FAKE_DOKKU_DIR/crontab"
"""
FAKE_SYSTEMCTL = """#!/bin/sh
echo "systemctl $@" >> "$FAKE_DOKKU_DIR/calls"
[ -f "$FAKE_DOKKU_DIR/timer" ] || { echo "Failed to get unit file state" >&2; exit 1; }
cat "$FAKE_DOKKU_DIR/timer"
[ "$(cat "$FAKE_DOKKU_DIR/timer")" = enabled ]
"""


@pytest.fixture
def fake_dokku(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDokku:
    """Fake `dokku`, `plugn`, `systemctl` and `crontab` commands, first in `PATH`; they record their calls in `calls`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = {"dokku": FAKE_DOKKU, "plugn": FAKE_PLUGN, "systemctl": FAKE_SYSTEMCTL, "crontab": FAKE_CRONTAB}
    for name, content in commands.items():
        script = bin_dir / name
        script.write_text(content)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    state_dir = tmp_path / "fake-dokku"
    state_dir.mkdir()
    lib_root = tmp_path / "var-lib-dokku"  # Same as `dokku_env`
    lock_file = lib_root / "data" / "apps" / FAKE_APP / ".deploy.lock"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOKKU_DIR", str(state_dir))
    monkeypatch.setenv("FAKE_LOCK_FILE", str(lock_file))
    monkeypatch.setenv("DOKKU_LIB_ROOT", str(lib_root))
    return FakeDokku(state_dir, lock_file)


class DokkuEnv:
    """Dokku's directories for the plugin: properties under `lib_root`, `.netrc` under `home` (the dokku user's)."""

    def __init__(self, tmp_path: Path) -> None:
        self.lib_root = tmp_path / "var-lib-dokku"
        self.home = tmp_path / "home-dokku"
        self.home.mkdir()
        self.properties = Properties(self.lib_root / "config" / "auto-deploy")

    def git_auth(self, host: str, password: str, login: str = "bot") -> None:
        with (self.home / ".netrc").open("a") as netrc:
            netrc.write(f"machine {host}\nlogin {login}\npassword {password}\n")

    def configure(self, app: str, **settings: str) -> None:
        """`configure("app", telegram_chat="-1")` is `dokku auto-deploy:set app telegram-chat -1` for each key."""
        for key, value in settings.items():
            self.properties.set(app, key.replace("_", "-"), value)


@pytest.fixture
def dokku_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DokkuEnv:
    env = DokkuEnv(tmp_path)
    monkeypatch.setenv("DOKKU_LIB_ROOT", str(env.lib_root))
    monkeypatch.setenv("DOKKU_ROOT", str(env.home))
    return env
