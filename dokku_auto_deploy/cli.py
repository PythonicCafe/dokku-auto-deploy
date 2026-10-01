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
        f"{EXIT_CONFIG} invalid settings, {EXIT_INTERRUPTED} interrupted.",
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
        help="Show settings and last deploy of the configured apps, or of one app",
        description="Show the resolved settings (with where each value comes from) and the state of the last "
        "handled commit. Without arguments, shows the global settings and every configured app.",
    )
    report.add_argument("target", metavar="app|--global", nargs="?", help="Only this app, or only global settings")

    poll = commands.add_parser(
        f"{PREFIX}poll",
        help="Check every configured app once and deploy what passed CI",
        description="Check every app with a repository set and deploy its branch head once its CI passed. A head "
        "already handled (deployed, failed CI, failed deploy) is skipped until the branch moves.",
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
    """`set` and `report` only take positionals, and some of them start with a dash: `--global` and Telegram chat
    ids like `-100123_45` (argparse only accepts plain negative numbers as values). A `--` after the command makes
    argparse read them as values; `-h`/`--help` still work."""
    only_positionals = (f"{PREFIX}set", f"{PREFIX}report")
    if argv and argv[0] in only_positionals and not {"-h", "--help"} & set(argv[1:]):
        argv = [argv[0], "--", *argv[1:]]
    return create_parser().parse_args(argv)


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


def _report_global(properties: Properties) -> None:
    from dokku_auto_deploy.settings import KEYS

    rows = []
    for key in KEYS.values():
        if key.scope == "app":
            continue
        value = properties.get(GLOBAL, key.name)
        if key.secret:
            rows.append((key.name, "set" if value else "not set"))
        else:
            rows.append((key.name, value or ""))
    _print_rows("auto-deploy global settings", rows)


def _report_app(properties: Properties, app: str) -> None:
    from dokku_auto_deploy.poll import load_state
    from dokku_auto_deploy.settings import KEYS, ConfigError, load_app, value_and_origin

    rows = []
    for key in KEYS.values():
        if key.scope == "global":
            continue
        value, origin = value_and_origin(properties, app, key.name)
        rows.append((key.name, f"{value} (global)" if origin == "global" else value or ""))
    try:
        load_app(properties, app)
    except ConfigError as exc:
        rows.append(("problem", str(exc)))
    state = load_state(properties, app) or {}
    rows.append(("last handled", " ".join(str(state.get(field) or "") for field in ("sha", "status", "at")).strip()))
    rows.append(("last deployed", str(state.get("deployed_sha") or "")))
    _print_rows(f"{app} auto-deploy information", rows)


def _report(properties: Properties, target: str | None) -> int:
    from dokku_auto_deploy.settings import configured_apps

    if target == GLOBAL:
        _report_global(properties)
        return EXIT_OK
    if target is not None:
        if properties.get(target, "repository") is None:
            print(f"Error: auto-deploy is not configured for {target} (no repository set)", file=sys.stderr)
            return EXIT_CONFIG
        _report_app(properties, target)
        return EXIT_OK
    _report_global(properties)
    for app in configured_apps(properties):
        _report_app(properties, app)
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
            errors = poll(
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
    return EXIT_ERROR if errors else EXIT_OK


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
            return _report(properties, args.target)
        if command == "poll":
            return _poll(properties, args.redeploy)
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
