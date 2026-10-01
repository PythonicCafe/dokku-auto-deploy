# AGENTS.md

> `dokku-auto-deploy` is a Dokku plugin (`auto-deploy`) with stdlib-only Python code that deploys each app from its
> forge branch once CI passes, so the forge never holds credentials to the server.

## Commands

- Everything before a commit: `make check` (ruff check, ruff format --check, shellcheck, mypy --strict, pytest)
- Auto-fix style: `make lint`
- Quick loop: `make test-fast`

## Conventions

- No runtime dependencies and no packaging: Dokku runs the code in place (`subcommands/default` runs
  `dokku_auto_deploy.cli.main` with `python3 -I`), so it must work with the host's `python3` (3.11+). HTTP goes through
  `urllib.request` with an explicit timeout. If something seems to need a library, ask first.
- Plugin commands run as the dokku user; only the `install`/`update`/`uninstall` triggers run as root. Anything that
  needs root (e.g. systemd units) belongs in those triggers, not in a command.
- Dokku reads `--force`, `--app`, `--quiet` and `--trace` anywhere on the command line: don't use them as option
  names; pick another name (`poll --redeploy`).
- Dokku runs `subcommands/<name>` for `dokku auto-deploy:<name>`: a new command needs a `subcommands/<name>` symlink to
  `default` and a line in `help-functions` (a test checks both).
- Only `cli.py` writes to stdout/stderr. Other modules log through `logging.getLogger(__name__)`, and build output
  reaches the terminal through the `on_output` callback. Data (build log, `report`) goes to stdout, status to stderr.
- Secrets never go in CLI arguments, logs or `report`: secret settings are read from stdin (`set --global
  telegram-bot-token`), and forge tokens come from the dokku user's `.netrc` (`dokku git:auth`). The Telegram URL
  contains the bot token: never log a request URL or an exception that includes it (`Telegram.send_message` shows
  how).
- Settings live in Dokku's property store, in Dokku's layout (`properties.py`). A new key goes in `settings.KEYS`
  (scope, description, validation) and in the README settings table in the same commit.
- A new forge implements `forge.Forge` in its own module and is registered in `forges.py` and `repository.FORGES`.
- Notifications are best-effort: a failing channel is reported and never changes the deploy status.
- User-facing text (CLI, logs, comments, Telegram) is in English.
- Bash scripts follow Dokku's style (`set -eo pipefail`, `declare desc=...` functions) and must pass shellcheck.

## Tests

- Test behavior through the real code paths: `tests/conftest.py` provides `fake_api` (a local HTTP server for the
  forge APIs and Telegram that records every request), `fake_dokku` (a `dokku` script put first in `PATH`) and
  `dokku_env` (Dokku's directories in a temp dir, with `git_auth` and `configure` helpers). Use them instead of mocking
  `urllib` or `subprocess`.
- The bash entry points are tested by running them (`run_plugin` in `tests/test_cli.py`).
- Tests never touch the network or a real Dokku.
- Group related tests in plain `class TestX:`; use `pytest.mark.parametrize` for case tables.

## Vocabulary

- Configured app: an app with a `repository` setting.
- Handled SHA (`sha` in the state): the last branch head the plugin acted on, whatever the result. `deployed_sha`: the
  last one that deployed successfully; it bounds which merged changes get notified.
- Change: a merged pull request (GitHub, Forgejo) or merge request (GitLab).
- Locked app: `dokku apps:locked` is true (a deploy in progress, manual or not, or a manual `apps:lock`). The plugin
  waits; it never unlocks.

## Git

- Gitflow: branch `feature/<name>` from `develop` or `hotfix/<name>` from `main`, and open a PR. Never commit to
  `main` or `develop`.
- Commit titles in English, present tense ("Adds", "Fixes"), names in backticks.
- A release bumps `version` in `plugin.toml` and `__version__` together (a test checks it) and is installed by tag (no
  `v` prefix: `0.2.0`).

## References

- Why things are the way they are (pull vs push, plugin vs package, Dokku lock behavior, settings, tokens):
  `docs/design-decisions.md`. Update it when you make or discover a design decision.

If during a session you find a wrong assumption in this file, suggest the fix.
