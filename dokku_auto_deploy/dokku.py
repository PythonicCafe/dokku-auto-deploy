"""Calls to the local `dokku` CLI. Runs on the Dokku host itself, so no SSH key is involved."""

import signal
import subprocess
import threading
from collections.abc import Callable

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


def is_locked(app: str) -> bool:
    """True while Dokku holds the app's deploy lock: a deploy in progress (from anyone) or a manual `apps:lock`."""
    result = subprocess.run(["dokku", "apps:locked", app], capture_output=True, check=False, timeout=LOCK_CHECK_TIMEOUT)
    return result.returncode == 0


def deployed_rev(app: str) -> str | None:
    """Commit the app was last built from, per the `GIT_REV` config var Dokku sets on every git deploy; None if unset.

    Dokku sets it right before building, so after a failed build it names the commit that failed; callers only use it
    to skip rebuilding a commit that is already there. Apps with `git:set <app> rev-env-var ""` have no GIT_REV.
    """
    result = subprocess.run(
        ["dokku", "config:get", app, "GIT_REV"], capture_output=True, text=True, check=False, timeout=LOCK_CHECK_TIMEOUT
    )
    rev = result.stdout.strip()
    return rev if result.returncode == 0 and rev else None


def app_url(app: str) -> str | None:
    """First URL of the app (`dokku url <app>`, from its domains and proxy settings); None if it has none."""
    result = subprocess.run(
        ["dokku", "url", app], capture_output=True, text=True, check=False, timeout=LOCK_CHECK_TIMEOUT
    )
    for line in result.stdout.splitlines():
        if line.strip().startswith(("http://", "https://")):
            return line.strip()
    return None


def git_sync(
    app: str, repository: str, sha: str, on_output: Callable[[bytes], object] | None = None
) -> tuple[bool, str]:
    """Fetch the exact `sha` from GitHub into the app repo and build it (`git:sync --build`); returns (ok, output).

    With an explicit SHA, `git:sync` moves the deploy branch with `update-ref`, so it works even after someone
    force-pushed another history to the app by hand.
    """
    command = ["dokku", "git:sync", "--build", app, f"https://github.com/{repository}.git", sha]
    returncode, output = run_streaming(command, DEPLOY_TIMEOUT, on_output)
    return returncode == 0, output
