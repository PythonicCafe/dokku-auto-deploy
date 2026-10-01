"""Calls to the local `dokku` CLI (the plugin runs as the dokku user on the Dokku host: no SSH key is involved)."""

import json
import signal
import subprocess
import threading
from collections.abc import Callable
from typing import Literal

from dokku_auto_deploy.properties import lib_root

DEPLOY_TIMEOUT = 60 * 60
LOCK_CHECK_TIMEOUT = 60


def run_streaming(
    command: list[str], timeout: int, on_output: Callable[[bytes], object] | None = None
) -> tuple[int, str]:
    """Run `command`, passing each output line to `on_output` as it arrives and also returning the whole output.

    stderr is merged into stdout (build logs interleave both). The process is killed after `timeout` seconds; the
    returned output then ends with a note saying so. `subprocess.run(timeout=...)` can't be used because it only
    returns the output at the end, and a deploy log must be visible (journald) while the build runs.
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    killer = threading.Timer(timeout, process.kill)
    killer.start()
    chunks = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            chunks.append(line)
            if on_output is not None:
                on_output(line)
        returncode = process.wait()
    finally:
        killer.cancel()
    output = b"".join(chunks).decode("utf-8", errors="replace")
    if returncode == -signal.SIGKILL:
        output += f"\nTimeout: process killed after {timeout}s\n"
    return returncode, output


def app_exists(app: str) -> bool:
    result = subprocess.run(["dokku", "apps:exists", app], capture_output=True, check=False, timeout=LOCK_CHECK_TIMEOUT)
    return result.returncode == 0


LockState = Literal["free", "manual", "deploying", "orphan"]


def lock_state(app: str) -> LockState:
    """What the app's deploy lock file means (`<DOKKU_LIB_ROOT>/data/apps/<app>/.deploy.lock`).

    `apps:locked` only says whether the file exists, which isn't enough: Dokku leaves it behind when a build fails (its
    failure path exits before releasing the lock), and the file then blocks nothing but still looks locked. So:
    - no file: "free";
    - empty file: "manual", created by `apps:lock`;
    - a build id whose record in `builds:list` is running: "deploying";
    - a build id whose record isn't running (failed, abandoned): "orphan", a leftover that doesn't block deploys.
    A build id that can't be looked up (Dokku before 0.38 has no `builds:list`) counts as "deploying", the safe side.
    """
    path = lib_root() / "data" / "apps" / app / ".deploy.lock"
    try:
        build_id = path.read_text().strip()
    except FileNotFoundError:
        return "free"
    if not build_id:
        return "manual"
    result = subprocess.run(
        ["dokku", "builds:list", app, "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=LOCK_CHECK_TIMEOUT,
    )
    try:
        builds = json.loads(result.stdout) if result.returncode == 0 else []
    except ValueError:
        builds = []
    for build in builds if isinstance(builds, list) else []:
        if isinstance(build, dict) and build.get("id") == build_id:
            return "deploying" if build.get("display_status") == "running" else "orphan"
    return "deploying"


def is_locked(app: str) -> bool:
    """True while a deploy runs (from anyone) or the app is locked with `apps:lock`; an orphan lock file isn't."""
    return lock_state(app) in ("manual", "deploying")


def runs_commit(app: str, sha: str) -> bool:
    """Whether the app's last successful deploy was `sha`, per Dokku's `deploy-source-metadata`.

    Dokku only sets it after a deploy succeeds: `<sha>` for `git push`, `<remote>#<sha>` for `git:sync`. `GIT_REV`
    can't be used for this: Dokku sets it before building, so it also names commits whose deploy failed.
    """
    result = subprocess.run(
        ["dokku", "apps:report", app, "--app-deploy-source-metadata"],
        capture_output=True,
        text=True,
        check=False,
        timeout=LOCK_CHECK_TIMEOUT,
    )
    metadata = result.stdout.strip() if result.returncode == 0 else ""
    return bool(sha) and (metadata == sha or metadata.endswith(f"#{sha}"))


def app_url(app: str) -> str | None:
    """First URL of the app (`dokku url <app>`, from its domains and proxy settings); None if it has none."""
    try:
        result = subprocess.run(
            ["dokku", "url", app], capture_output=True, text=True, check=False, timeout=LOCK_CHECK_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return None
    for line in result.stdout.splitlines():
        if line.strip().startswith(("http://", "https://")):
            return line.strip()
    return None


def git_sync(
    app: str, clone_url: str, sha: str, on_output: Callable[[bytes], object] | None = None
) -> tuple[bool, str]:
    """Fetch the exact `sha` from `clone_url` into the app repo and build it (`git:sync --build`); returns (ok, output).

    With an explicit SHA, `git:sync` moves the deploy branch with `update-ref`, so it works even after someone
    force-pushed another history to the app by hand.
    """
    command = ["dokku", "git:sync", "--build", app, clone_url, sha]
    returncode, output = run_streaming(command, DEPLOY_TIMEOUT, on_output)
    return returncode == 0, output
