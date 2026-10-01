"""How `auto-deploy:poll` gets run every minute: Dokku's crontab or the systemd timer installed by the plugin.

Cron is fully managed from here: the `cron-entries` trigger adds the task to the crontab Dokku generates for its own
user whenever the global `schedule` property is `cron`, and `scheduler-cron-write` regenerates that crontab. The
systemd units belong to root (plugin commands run as the dokku user), so for systemd this can only report the timer's
state and say which `systemctl` command to run.
"""

import dataclasses
import os
import shutil
import subprocess
from pathlib import Path

from dokku_auto_deploy.properties import GLOBAL, Properties

SCHEDULE_KEY = "schedule"
TIMER = "dokku-auto-deploy.timer"
TIMEOUT = 60
CRON_COMMAND = "dokku auto-deploy:poll"  # As written by the `cron-entries` trigger


def log_file() -> Path:
    return Path(os.environ.get("DOKKU_LOGS_DIR", "/var/log/dokku")) / "auto-deploy.log"


@dataclasses.dataclass(frozen=True)
class Status:
    cron: bool
    timer: str  # "enabled", "disabled" or "not installed"
    timer_active: bool  # Running now; an enabled timer is inactive until started (or the next boot)


def timer_state() -> str:
    if shutil.which("systemctl") is None:
        return "not installed"
    result = subprocess.run(
        ["systemctl", "is-enabled", TIMER], capture_output=True, text=True, check=False, timeout=TIMEOUT
    )
    state = result.stdout.strip()
    if state in ("enabled", "enabled-runtime"):
        return "enabled"
    if state in ("disabled", "static", "indirect", "linked", "linked-runtime", "masked", "masked-runtime"):
        return "disabled"
    return "not installed"


def timer_active() -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(["systemctl", "is-active", TIMER], capture_output=True, check=False, timeout=TIMEOUT)
    return result.returncode == 0


def status(properties: Properties) -> Status:
    return Status(cron=properties.get(GLOBAL, SCHEDULE_KEY) == "cron", timer=timer_state(), timer_active=timer_active())


class CrontabError(RuntimeError):
    pass


def write_crontab() -> None:
    """Regenerate the dokku user's crontab from every `cron-entries` trigger (Dokku's cron plugin does the writing)."""
    try:
        subprocess.run(["plugn", "trigger", "scheduler-cron-write"], check=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CrontabError(f"could not regenerate the crontab (plugn trigger scheduler-cron-write): {exc}") from None


def crontab_has_task() -> bool:
    """Whether the dokku user's crontab (this runs as the dokku user) runs `auto-deploy:poll`."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CrontabError(f"could not read the crontab (crontab -l): {exc}") from None
    return result.returncode == 0 and any(
        CRON_COMMAND in line and not line.lstrip().startswith("#") for line in result.stdout.splitlines()
    )


def set_cron(properties: Properties, enabled: bool) -> None:
    """Turn the cron task on or off. The crontab is regenerated every time, so running it again repairs a crontab
    that a previous failure left behind.

    Dokku only writes the tasks of schedulers that use the host crontab, and only when some app uses one of them: on a
    server whose apps all run on k3s, for instance, the task never reaches the crontab. So turning it on checks the
    crontab, and undoes the change when the task isn't there.
    """
    if not enabled:
        properties.delete(GLOBAL, SCHEDULE_KEY)
        write_crontab()
        return
    properties.set(GLOBAL, SCHEDULE_KEY, "cron")
    write_crontab()
    if not crontab_has_task():
        properties.delete(GLOBAL, SCHEDULE_KEY)
        write_crontab()
        raise CrontabError(
            "Dokku didn't add the task to the crontab: it only writes tasks when some app uses a scheduler that runs "
            "on the host crontab (docker-local); use the systemd timer instead"
        )
