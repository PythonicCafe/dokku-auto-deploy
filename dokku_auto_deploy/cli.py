"""Command line of the `auto-deploy:*` Dokku commands: argument parsing, logging setup and dispatch.

`subcommands/<command>` runs `main` with `auto-deploy:<command> [args]` as the dokku user, so the first argument is the
full Dokku command name.
"""

import argparse
import logging
import os
import sys

from dokku_auto_deploy.properties import GLOBAL, Properties

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_DEPLOY_FAILED = 4
EXIT_INTERRUPTED = 130
PREFIX = "auto-deploy:"


def parse_change_number(value: str) -> int:
    """argparse type: `12`, `#12` or `!12` -> 12."""
    number = value.strip().removeprefix("#").removeprefix("!")
    if not number.isdigit() or int(number) == 0:
        raise argparse.ArgumentTypeError(f"expected a pull/merge request number like 12, got {value!r}")
    return int(number)


def create_parser() -> argparse.ArgumentParser:
    from dokku_auto_deploy import __version__
    from dokku_auto_deploy.settings import KEYS

    parser = argparse.ArgumentParser(
        prog="dokku",
        description="Deploy Dokku apps from forge branches once their CI passes",
        epilog=f"Exit codes: {EXIT_OK} ok, {EXIT_ERROR} error in at least one app, {EXIT_USAGE} invalid arguments, "
        f"{EXIT_CONFIG} invalid settings, {EXIT_DEPLOY_FAILED} (poll) at least one deploy failed and no other error, "
        f"{EXIT_INTERRUPTED} interrupted. A commit whose CI failed isn't an error.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"auto-deploy {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)

    keys = "\n".join(f"  {key.name:<20} [{key.scope}] {key.description}" for key in KEYS.values())
    set_ = commands.add_parser(
        f"{PREFIX}set",
        help="Set or unset a setting of an app, or a global one",
        description="Set KEY to VALUE for APP (or globally with --global); without VALUE, unset it. Keys that can "
        "be set in both places use the app's value when it has one, else the global one.",
        epilog=f"keys ([scope]):\n{keys}\n\nSecret keys are read from stdin, never from arguments "
        "(they would show up in `ps`):\n  cat token-file | dokku auto-deploy:set --global telegram-bot-token\n"
        "Run from a terminal, without input, it unsets the key.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    set_.add_argument("target", metavar="app|--global", help="App name, or --global")
    set_.add_argument("key", choices=list(KEYS), metavar="key", help="Setting name (see below)")
    set_.add_argument("value", nargs="?", help="New value; omit it to unset the key")

    report = commands.add_parser(
        f"{PREFIX}report",
        usage="dokku auto-deploy:report [<app>|--global] [--format stdout|json] [--auto-deploy-<name>]",
        help="Show settings and last handled commit of every configured app, of one app, or global settings",
        description="Show each app's settings (its own value, the global one and the one in effect, `computed`), "
        "the token's login and the last handled and deployed commits. Without an app, every configured app; with "
        "--global, the global settings and the schedule.",
        epilog="--auto-deploy-<name> prints only that value, e.g. --auto-deploy-computed-workflow. With --format "
        "json, all apps come as one object keyed by app name.",
    )
    report.add_argument("target", metavar="app", nargs="?", help="Only this app")
    report.add_argument("--format", choices=("stdout", "json"), default="stdout", help="Output format")

    poll = commands.add_parser(
        f"{PREFIX}poll",
        help="Check every configured app once and deploy what passed CI",
        description="Check every app with a repository set and deploy its branch head once its CI passed. A head "
        "already deployed, or whose deploy failed, is skipped until the branch moves; one whose CI failed is deployed if a "
        "re-run of its CI passes.",
    )
    poll.add_argument(
        "-r",
        "--redeploy",
        metavar="app",
        action="append",
        default=[],
        help="Deploy APP's branch head even if it was already handled or already runs (retry a failed deploy, "
        "undo a manual one); can be repeated",
    )
    poll.add_argument("-v", "--verbose", action="store_true", help="Also log apps waiting for CI")

    schedule = commands.add_parser(
        f"{PREFIX}schedule",
        help="Show or choose how poll runs every minute: systemd timer or Dokku's cron",
        description="Without an argument, show whether poll is scheduled by cron and by the systemd timer. cron adds "
        "a task to the crontab Dokku manages for the dokku user (log: /var/log/dokku/auto-deploy.log); systemd and "
        "none remove it. The systemd timer can only be enabled or disabled by root, so this prints the systemctl "
        "command to run.",
    )
    schedule.add_argument("scheduler", nargs="?", choices=("systemd", "cron", "none"), help="What should run poll")

    notify_test = commands.add_parser(
        f"{PREFIX}notify-test",
        help="Send a test Telegram message and, optionally, a test comment",
        description="Send a test message to APP's telegram-chat (if telegram is in its notify) and, with "
        "--pull-request, a test comment on that pull/merge request of APP's repository.",
    )
    notify_test.add_argument("app", help="Configured app")
    notify_test.add_argument(
        "-p",
        "--pull-request",
        metavar="number",
        type=parse_change_number,
        help="Also comment on this pull request (GitHub, Forgejo) or merge request (GitLab)",
    )
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Two Dokku conventions argparse doesn't know:

    - `set` only takes positionals, and some start with a dash: `--global` and Telegram chat ids like `-100123_45`
      (argparse only accepts plain negative numbers as values). A `--` after the command makes argparse read them as
      values; `-h`/`--help` still work.
    - `report` takes `--global` in place of the app, and any `--auto-deploy-<name>` flag to print one value.
    """
    parser = create_parser()
    if argv and argv[0] == f"{PREFIX}set" and not {"-h", "--help"} & set(argv[1:]):
        argv = [argv[0], "--", *argv[1:]]
    args, unknown = parser.parse_known_args(argv)
    args.info_flag = None
    if args.command == f"{PREFIX}report":
        for item in unknown:
            if item == GLOBAL and args.target is None:
                args.target = GLOBAL
            elif item.startswith("--auto-deploy-") and args.info_flag is None:
                args.info_flag = item
            else:
                parser.error(f"unrecognized argument: {item}")
    elif unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    return args


def _stdin_is_terminal() -> bool:
    return sys.stdin is None or sys.stdin.isatty()


def _set(properties: Properties, target: str, key: str, value: str | None) -> int:
    from dokku_auto_deploy import dokku
    from dokku_auto_deploy.settings import check_key

    spec = check_key(target, key)
    if target != GLOBAL and not dokku.app_exists(target):
        print(f"Error: app {target} does not exist", file=sys.stderr)
        return EXIT_CONFIG
    if spec.secret:
        if value is not None:
            print(
                f"Error: {key} is a secret: pass it through stdin, not as an argument (it would show up in `ps`): "
                f"cat token-file | dokku auto-deploy:set {target} {key}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        # Like `dokku git:auth`: the value comes from stdin (a pipe or a file). Only a run from a terminal unsets it:
        # an empty pipe is more likely a mistake (`cat wrong-file | ...`) than a request to delete a working token
        if not _stdin_is_terminal():
            value = sys.stdin.read().strip()
            if not value:
                print(f"Error: empty {key} on stdin (to unset it, run the command from a terminal)", file=sys.stderr)
                return EXIT_CONFIG
    if value is None or value == "":
        properties.delete(target, key)
        print(f"=====> Unsetting {key}")
        return EXIT_OK
    value = spec.normalize(value)
    properties.set(target, key, value)
    print(f"=====> Setting {key}" if spec.secret else f"=====> Setting {key} to {value}")
    return EXIT_OK


def _print_rows(title: str, rows: list[tuple[str, str]]) -> None:
    print(f"=====> {title}")
    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        print(f"       {label + ':':<{width}}{value}")


def _report(properties: Properties, target: str | None, output_format: str, info_flag: str | None) -> int:
    from dokku_auto_deploy.report import FLAG_PREFIX, app_report, flags, global_report, render_json, render_text
    from dokku_auto_deploy.settings import configured_apps

    if target is not None and target != GLOBAL and properties.get(target, "repository") is None:
        print(f"Error: auto-deploy is not configured for {target} (no repository set)", file=sys.stderr)
        return EXIT_CONFIG
    if target is None:
        if info_flag is not None:
            print("Error: --auto-deploy-<name> needs an app (or --global)", file=sys.stderr)
            return EXIT_USAGE
        reports = {app: app_report(properties, app) for app in configured_apps(properties)}
        if output_format == "json":
            print(render_json(reports))
        else:
            for app, values in reports.items():
                print(render_text(f"{app} auto-deploy information", values))
        return EXIT_OK
    values = global_report(properties) if target == GLOBAL else app_report(properties, target)
    if info_flag is not None:
        if info_flag not in flags(values):
            print(f"Error: invalid flag {info_flag}, valid flags: {', '.join(flags(values))}", file=sys.stderr)
            return EXIT_USAGE
        print(values[info_flag.removeprefix(FLAG_PREFIX)])
    elif output_format == "json":
        print(render_json(values))
    else:
        title = "auto-deploy global information" if target == GLOBAL else f"{target} auto-deploy information"
        print(render_text(title, values))
    return EXIT_OK


def _poll(properties: Properties, redeploy: list[str]) -> int:
    from dokku_auto_deploy.poll import PollRunning, poll, poll_lock
    from dokku_auto_deploy.properties import data_dir
    from dokku_auto_deploy.settings import configured_apps
    from dokku_auto_deploy.telegram import DEFAULT_API as TELEGRAM_API

    apps = configured_apps(properties)
    unknown = sorted(set(redeploy) - set(apps))
    if unknown:
        print(f"Error: --redeploy: auto-deploy is not configured for: {', '.join(unknown)}", file=sys.stderr)
        return EXIT_CONFIG

    def write_output(chunk: bytes) -> None:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    try:
        with poll_lock(data_dir() / "poll.lock"):
            result = poll(
                properties,
                redeploy=redeploy,
                telegram_api=os.environ.get("DOKKU_AUTO_DEPLOY_TELEGRAM_API", TELEGRAM_API),
                on_output=write_output,
            )
    except PollRunning as exc:
        if redeploy:  # Someone is waiting for this one: don't drop it silently
            print(f"Error: {exc}; run the redeploy again when it finishes", file=sys.stderr)
            return EXIT_ERROR
        logging.getLogger(__name__).info("%s, skipping this run", exc)
        return EXIT_OK
    if result.errors:  # A tool error hides a failed deploy: it needs fixing before anything else
        return EXIT_ERROR
    return EXIT_DEPLOY_FAILED if result.failed_deploys else EXIT_OK


def _schedule_rows(properties: Properties) -> list[tuple[str, str]]:
    from dokku_auto_deploy.schedule import log_file, status

    current = status(properties)
    cron = f"on, every minute, log: {log_file()}" if current.cron else "off"
    timer = current.timer
    if timer != "not installed":
        timer += ", active" if current.timer_active else ", not active"
    return [("cron", cron), ("systemd timer", timer)]


def _schedule(properties: Properties, scheduler: str | None) -> int:
    from dokku_auto_deploy.schedule import TIMER, CrontabError, set_cron, timer_active, timer_state

    if scheduler is None:
        _print_rows("auto-deploy schedule", _schedule_rows(properties))
        return EXIT_OK
    timer = timer_state()
    if scheduler == "systemd" and timer == "not installed":
        print(f"Error: {TIMER} is not installed (no systemd?); use cron instead", file=sys.stderr)
        return EXIT_CONFIG
    try:
        set_cron(properties, scheduler == "cron")
    except CrontabError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"=====> cron {'on' if scheduler == 'cron' else 'off'}")
    active = timer_active()
    if scheduler == "systemd" and (timer != "enabled" or not active):
        print(f"Now enable and start the timer as root: systemctl enable --now {TIMER}")
    elif scheduler != "systemd" and (timer == "enabled" or active):
        print(f"The systemd timer is on too; stop and disable it as root: systemctl disable --now {TIMER}")
    return EXIT_OK


def _notify_test(properties: Properties, app: str, number: int | None) -> int:
    from dokku_auto_deploy.forge import ForgeError
    from dokku_auto_deploy.notify import send_test_comment, send_test_message
    from dokku_auto_deploy.poll import forge_for
    from dokku_auto_deploy.settings import load_app
    from dokku_auto_deploy.telegram import DEFAULT_API as TELEGRAM_API
    from dokku_auto_deploy.telegram import TelegramError

    if properties.get(app, "repository") is None:
        print(f"Error: auto-deploy is not configured for {app} (no repository set)", file=sys.stderr)
        return EXIT_CONFIG
    config = load_app(properties, app)
    if "telegram" not in config.notify and number is None:
        print(
            f"Error: nothing to test: {app} doesn't have telegram in notify; "
            f"pass --pull-request <number> to test comments",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    failed = False
    if "telegram" in config.notify:
        token = properties.get(GLOBAL, "telegram-bot-token")
        if token is None:
            print(
                "Error: no Telegram bot token (cat token-file | dokku auto-deploy:set --global telegram-bot-token)",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        try:
            send_test_message(config, token, os.environ.get("DOKKU_AUTO_DEPLOY_TELEGRAM_API", TELEGRAM_API))
            print(f"ok telegram {config.telegram_chat}")
        except TelegramError as exc:
            failed = True
            print(f"failed telegram {config.telegram_chat}: {exc}", file=sys.stderr)
    elif "comment" in config.notify:
        print("Telegram not tested: telegram is not in notify", file=sys.stderr)
    if number is not None:
        forge = forge_for(config)
        try:
            send_test_comment(config, forge, number)
            print(f"ok comment {config.repository.url} {number}")
        except ForgeError as exc:
            failed = True
            print(f"failed comment {config.repository.url} {number}: {exc}", file=sys.stderr)
    elif "comment" in config.notify:
        print("Comments not tested: pass --pull-request <number>", file=sys.stderr)
    return EXIT_ERROR if failed else EXIT_OK


def _setup_logging(verbose: bool) -> None:
    """Timestamps only when nothing else adds them: journald (systemd sets INVOCATION_ID) and terminals don't need
    them; a log file written by cron does."""
    timestamps = "INVOCATION_ID" not in os.environ and not sys.stderr.isatty()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=("%(asctime)s " if timestamps else "") + "%(levelname)s %(message)s",
        stream=sys.stderr,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    from dokku_auto_deploy.settings import ConfigError

    args = parse_args(sys.argv[1:] if argv is None else argv)
    _setup_logging(getattr(args, "verbose", False))
    properties = Properties()
    command = args.command.removeprefix(PREFIX)
    try:
        if command == "set":
            return _set(properties, args.target, args.key, args.value)
        if command == "report":
            return _report(properties, args.target, args.format, args.info_flag)
        if command == "poll":
            return _poll(properties, args.redeploy)
        if command == "schedule":
            return _schedule(properties, args.scheduler)
        return _notify_test(properties, args.app, args.pull_request)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except PermissionError as exc:
        print(
            f"Error: permission denied: {exc.filename}. Run it through dokku (dokku auto-deploy:...), which runs "
            f"plugin commands as the dokku user.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
