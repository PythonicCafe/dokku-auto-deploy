"""Calls to the local `dokku` CLI (the plugin runs as the dokku user on the Dokku host: no SSH key is involved)."""

import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from typing import Literal

from dokku_auto_deploy.properties import lib_root

DEPLOY_TIMEOUT = 60 * 60
LOCK_CHECK_TIMEOUT = 60
INTERRUPT_GRACE = 30

logger = logging.getLogger(__name__)


def _signal_group(process: subprocess.Popen[bytes], signum: int) -> None:
    with contextlib.suppress(ProcessLookupError):  # Already gone
        os.killpg(process.pid, signum)


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Interrupt the process group like Ctrl+C would, and kill it if it's still there after `INTERRUPT_GRACE` seconds."""
    _signal_group(process, signal.SIGINT)  # Its own session doesn't get the terminal's Ctrl+C: pass it on
    try:
        process.wait(timeout=INTERRUPT_GRACE)
    except subprocess.TimeoutExpired:
        logger.warning("Build still running %ss after the interrupt, killing it", INTERRUPT_GRACE)
    _signal_group(process, signal.SIGKILL)  # Children may outlive `dokku` itself
    process.wait()


def run_streaming(
    command: list[str], timeout: int, on_output: Callable[[bytes], object] | None = None
) -> tuple[int, str]:
    """Run `command`, passing each output line to `on_output` as it arrives and also returning the whole output.

    stderr is merged into stdout (build logs interleave both). After `timeout` seconds the whole process group is
    killed: `dokku` runs the build in child processes that keep the output pipe open, so killing only `dokku` would
    leave this waiting for them. The returned output then ends with a note saying so. `subprocess.run(timeout=...)`
    can't be used because it only returns the output at the end, and a deploy log must be visible while it runs.

    The build is never left running unsupervised: if `on_output` fails (e.g. the terminal went away), forwarding stops
    but the build is still followed to its end; if this is interrupted (Ctrl+C, any other exception), the build is
    stopped before the exception goes on.
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        _signal_group(process, signal.SIGKILL)

    killer = threading.Timer(timeout, kill_on_timeout)
    killer.start()
    chunks = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            chunks.append(line)
            if on_output is not None:
                try:
                    on_output(line)
                except Exception:
                    logger.warning("Can't show the build output anymore, following the build without it", exc_info=True)
                    on_output = None
        returncode = process.wait()
    except BaseException:
        _stop(process)
        raise
    finally:
        killer.cancel()
    output = b"".join(chunks).decode("utf-8", errors="replace")
    if timed_out.is_set():
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

    Dokku writes it only when a deploy succeeds: `<sha>` for a `git push`, `<remote>#<sha>` for `git:sync`. Any other
    value (empty, an image or archive deploy) means "not known to run it". `GIT_REV` can't be used: Dokku sets it
    before building, so it also names commits whose deploy failed.
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
) -> tuple[bool, str, bool]:
    """Fetch the exact `sha` from `clone_url` into the app repo and build it (`git:sync --build`).

    Returns (ok, output, timed out). With an explicit SHA, `git:sync` moves the deploy branch with `update-ref`, so it
    works even after someone force-pushed another history to the app by hand. A deploy killed by the timeout can't
    remove Dokku's deploy lock file, so the app stays locked until `apps:unlock`; the output says so.
    """
    command = ["dokku", "git:sync", "--build", app, clone_url, sha]
    returncode, output = run_streaming(command, DEPLOY_TIMEOUT, on_output)
    timed_out = returncode == -signal.SIGKILL
    if timed_out:
        output += f"The app may stay locked; after checking no deploy runs: dokku apps:unlock {app}\n"
    return returncode == 0, output, timed_out
