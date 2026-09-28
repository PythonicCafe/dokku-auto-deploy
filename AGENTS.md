# AGENTS.md

> `dokku-auto-deploy` is a stdlib-only Python CLI that runs on a Dokku host and deploys GitHub branches (stg/prd)
> once their CI passes, so GitHub never holds credentials to the server.

## Commands

- Everything before a commit: `make check` (ruff check, ruff format --check, mypy --strict, pytest)
- Auto-fix style: `make lint`
- Quick loop: `make test-fast`
- Packaging: `make build-check` (builds, installs the wheel in a temp venv, runs the tests from the sdist, twine check)

## Conventions

- No runtime dependencies. HTTP goes through `urllib.request` with an explicit timeout; config is read with `tomllib`.
  If something seems to need a library, ask first.
- Only `cli.py` writes to stdout/stderr. Other modules log through `logging.getLogger(__name__)`, and build output
  reaches the terminal through the `on_output` callback. Data (build log, `config show`) goes to stdout, status to
  stderr.
- Secrets never go in CLI arguments, config values or logs; the config only holds paths to token files. The Telegram
  URL contains the bot token: never log a request URL or an exception that includes it (`Telegram.send_message`
  shows how).
- Config keys: a missing key and an empty string mean the same thing. A new key must be added to the `*_KEYS` tuple,
  `CONFIG_TEMPLATE` and the README config reference in the same commit.
- Notifications are best-effort: a failing channel is reported and never changes the deploy status.
- User-facing text (CLI, logs, GitHub comments, Telegram) is in English.

## Tests

- Test behavior through the real code paths: `tests/conftest.py` provides `fake_api` (a local HTTP server for GitHub
  and Telegram that records every request) and `fake_dokku` (a `dokku` script put first in `PATH`). Use them instead
  of mocking `urllib` or `subprocess`.
- Tests never touch the network or a real Dokku.
- Group related tests in plain `class TestX:`; use `pytest.mark.parametrize` for case tables.

## Vocabulary

- Target: one environment of one repository (repo + branch -> Dokku app).
- Handled SHA (`sha` in the state file): the last branch head the tool acted on, whatever the result.
  `deployed_sha`: the last one that deployed successfully; it bounds which merged PRs get notified.
- Locked app: `dokku apps:locked` is true (a deploy in progress, manual or not, or a manual `apps:lock`). The tool
  waits; it never unlocks.

## Git

- Gitflow: branch `feature/<name>` from `develop` or `hotfix/<name>` from `main`, and open a PR. Never commit to
  `main` or `develop`.
- Commit titles in English, present tense ("Adds", "Fixes"), names in backticks.

If during a session you find a wrong assumption in this file, suggest the fix.
