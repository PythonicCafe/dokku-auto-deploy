# AGENTS.md

> `dokku-auto-deploy` is a Dokku plugin (`auto-deploy`) whose stdlib-only Python code runs on the Dokku host and
> deploys each app from its forge branch (GitHub, GitLab, Forgejo) once CI passes, so the forge never holds
> credentials to the server.

## Commands

- Everything before a commit: `make check` (ruff check, ruff format --check, shellcheck, mypy --strict, pytest)
- Auto-fix style: `make lint`
- Quick loop: `make test-fast`

## Git flow

- `main` is the default branch and only holds releases: `dokku plugin:install <url>` without `--committish` installs
  it, so it must always be stable. Never commit to `main` or `develop`, and never start work from `main`.
- Start features and fixes from `develop` (`feature/<name>`), rebase on the latest `develop` before a pull request,
  and target `develop`. Urgent fixes to a release: `hotfix/<version>` from `main`.
- Commits: one coherent intention each; a preparatory refactor in its own commit, before the feature. Stage files
  deliberately (`git add <file>` or `git add -p`) instead of `git add .`.
- Commit titles in English, imperative ("Add", "Fix"), names in backticks. Prefer no body; when needed, one or two
  plain sentences on why.
- Releases are tagged without a `v` prefix (`0.2.0`), with `version` in `plugin.toml` and `__version__` equal (a
  test checks it).

## Conventions

- No runtime dependencies and no packaging: Dokku runs the code in place (`subcommands/default` runs
  `dokku_auto_deploy.cli.main` with `python3 -I`), so it must work with the host's `python3` (3.11+). HTTP goes
  through `urllib.request`. If something seems to need a library, ask first.
- Every network call and subprocess has an explicit timeout; network errors become `ForgeError`/`TelegramError`.
- `pathlib.Path`, never `os.path`. Modern type hints (`str | None`). No one-letter names (`for app in apps`).
- No `except: pass`: handle or log. `except Exception` only where one app's failure must not stop the others, and
  `KeyboardInterrupt` exits with 130.
- Plugin commands run as the dokku user; only the `install`/`update`/`uninstall` triggers run as root. Anything that
  needs root (e.g. systemd units) belongs in those triggers, not in a command.
- Dokku reads `--force`, `--app`, `--quiet` and `--trace` anywhere on the command line: don't use them as option
  names; pick another name (`poll --redeploy`).
- Dokku runs `subcommands/<name>` for `dokku auto-deploy:<name>`: a new command needs a `subcommands/<name>` symlink
  to `default` and a line in `help-functions` (a test checks both).
- Only `cli.py` writes to stdout/stderr. Other modules log through `logging.getLogger(__name__)`, and build output
  reaches the terminal through the `on_output` callback. Data (build log, `report`) goes to stdout, status to stderr.
- Secrets never go in CLI arguments, logs, exception messages or `report`: secret settings are read from stdin
  (`set --global telegram-bot-token`), and forge tokens come from the dokku user's `.netrc` (`dokku git:auth`). The
  Telegram URL contains the bot token: never log a request URL or an exception that includes it
  (`Telegram.send_message` shows how).
- Settings live in Dokku's property store, in Dokku's layout (`properties.py`). A new key goes in `settings.KEYS`
  (scope, description, validation) and in the README settings table in the same commit.
- A new forge implements `forge.Forge` in its own module and is registered in `forges.py` and `repository.FORGES`.
  Every URL it builds comes from the repository URL, never from a canonical host.
- Notifications are best-effort: a failing channel is reported and never changes the deploy status.
- Everything in the repository is in English: code, user-facing text (CLI, logs, comments, Telegram), docs, commits.
- Bash scripts follow Dokku's style (`set -eo pipefail`, `declare desc=...` functions) and must pass shellcheck; keep
  logic in Python.

## Tests

- Test behavior through the real code paths: `tests/conftest.py` provides `fake_api` (a local HTTP server for the
  forge APIs and Telegram that records every request), `fake_dokku` (fake `dokku`, `plugn` and `systemctl` first in
  `PATH`) and `dokku_env` (Dokku's directories in a temp dir, with `git_auth` and `configure` helpers). Use them
  instead of mocking `urllib` or `subprocess`.
- The bash entry points are tested by running them (`run_plugin` in `tests/test_cli.py`).
- Tests never touch the network, a real Dokku or the host's systemd.
- Plain `assert`, no `TestCase`; group related tests in plain `class TestX:`; use `pytest.mark.parametrize` for case
  tables. A test added for a bug says so in its docstring ("Regression: ...").

## Vocabulary

- Configured app: an app with a `repository` setting.
- Handled SHA (`sha` in the state): the last branch head the plugin acted on, whatever the result. `deployed_sha`: the
  last one that deployed successfully; it bounds which merged changes get notified.
- Change: a merged pull request (GitHub, Forgejo) or merge request (GitLab).
- Locked app: `dokku apps:locked` is true (a deploy in progress, manual or not, or a manual `apps:lock`). The plugin
  waits; it never unlocks.

## References

- Details and reasons for the conventions above (branches, commits, CLI, secrets, tests, Markdown style):
  `docs/development.md`.
- Why the plugin works the way it does (pull vs push, plugin vs package, Dokku lock behavior, settings, tokens):
  `docs/design-decisions.md`. Update it when you make or discover a design decision.

If during a session you find a wrong assumption in this file, suggest the fix.
