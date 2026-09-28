"""Command-line interface: argument parsing, logging setup and dispatch (no deploy logic here)."""

import argparse
import logging
import os
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 3


def create_parser() -> argparse.ArgumentParser:
    from dokku_auto_deploy import __version__
    from dokku_auto_deploy.config import DEFAULT_CONFIG_PATH

    parser = argparse.ArgumentParser(
        prog="dokku-auto-deploy",
        description="Deploy Dokku apps from GitHub branches once their CI passes (runs on the Dokku host)",
        epilog=f"Exit codes: {EXIT_OK} ok, {EXIT_ERROR} error in at least one target, 2 invalid arguments, "
        f"{EXIT_CONFIG} invalid config or missing secret, 130 interrupted.",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="path",
        type=Path,
        default=Path(os.environ.get("DOKKU_AUTO_DEPLOY_CONFIG", DEFAULT_CONFIG_PATH)),
        help=f"Config file (default: {DEFAULT_CONFIG_PATH}, or DOKKU_AUTO_DEPLOY_CONFIG)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Also log targets waiting for CI")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)

    poll = commands.add_parser("poll", help="Check every target once and deploy what passed CI")
    poll.add_argument(
        "-f",
        "--force",
        metavar="app",
        action="append",
        default=[],
        help="Deploy APP's branch head even if it was already handled (retry a failed deploy, undo a manual one); "
        "can be repeated",
    )

    config = commands.add_parser("config", help="Create or inspect the config file")
    config_commands = config.add_subparsers(dest="config_command", metavar="action", required=True)
    init = config_commands.add_parser("init", help="Write a commented config template to the config path")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file")
    config_commands.add_parser("show", help="Validate the config and print the resolved targets")
    return parser


def _read_secret(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None


def _config_init(path: Path, force: bool) -> int:
    from dokku_auto_deploy.config import CONFIG_TEMPLATE

    if path.exists() and not force:
        print(f"Error: {path} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_ERROR
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE)
    return EXIT_OK


def _config_show(path: Path) -> int:
    from dokku_auto_deploy.config import load_config

    for target in load_config(path).targets:
        channels = ", ".join(
            f"telegram {target.telegram_chat}" if channel == "telegram" else channel for channel in target.notify
        )
        print(
            f"{target.repository} {target.branch} -> {target.app} "
            f"(workflow: {target.workflow or 'none, no CI wait'}, notify: {channels or '-'})"
        )
    return EXIT_OK


def _poll(path: Path, force_apps: list[str]) -> int:
    from dokku_auto_deploy.config import load_config
    from dokku_auto_deploy.github import DEFAULT_API as GITHUB_API
    from dokku_auto_deploy.poll import poll
    from dokku_auto_deploy.telegram import DEFAULT_API as TELEGRAM_API

    config = load_config(path)
    if not config.targets:
        print(f"Error: no repositories configured in {path}", file=sys.stderr)
        return EXIT_CONFIG
    unknown = sorted(set(force_apps) - {target.app for target in config.targets})
    if unknown:
        print(f"Error: --force: unknown app(s): {', '.join(unknown)}", file=sys.stderr)
        return EXIT_CONFIG
    github_token = _read_secret(config.settings.github_token_file)
    if github_token is None:
        print(f"Error: GitHub token file missing or empty: {config.settings.github_token_file}", file=sys.stderr)
        return EXIT_CONFIG
    telegram_token = None
    if any("telegram" in target.notify for target in config.targets):
        telegram_token = _read_secret(config.settings.telegram_token_file)
        if telegram_token is None:
            print(
                f"Error: Telegram token file missing or empty: {config.settings.telegram_token_file}",
                file=sys.stderr,
            )
            return EXIT_CONFIG

    def write_output(chunk: bytes) -> None:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    errors = poll(
        config,
        github_token=github_token,
        telegram_token=telegram_token,
        force_apps=force_apps,
        github_api=os.environ.get("DOKKU_AUTO_DEPLOY_GITHUB_API", GITHUB_API),
        telegram_api=os.environ.get("DOKKU_AUTO_DEPLOY_TELEGRAM_API", TELEGRAM_API),
        on_output=write_output,
    )
    return EXIT_ERROR if errors else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from dokku_auto_deploy.config import ConfigError

    args = create_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
    try:
        if args.command == "poll":
            return _poll(args.config, args.force)
        if args.config_command == "init":
            return _config_init(args.config, args.force)
        return _config_show(args.config)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
