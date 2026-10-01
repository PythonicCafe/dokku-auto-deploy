# Development guide

Conventions for changing this repository, for people and coding agents. `AGENTS.md` has the short version; this file
has the details and the reasons. Why the plugin itself works the way it does is in `design-decisions.md`.

## Branches and releases

The repository follows [git flow](https://nvie.com/posts/a-successful-git-branching-model/):

| Branch | Holds | Created from | Merged into |
|---|---|---|---|
| `main` (default) | Released code only; every commit on it is a release, tagged | - | - |
| `develop` | The next release, integrated and passing `make check` | - | - |
| `feature/<name>` | One feature or fix | `develop` | `develop`, through a pull request |
| `release/<version>` | Version bump and last fixes before a release | `develop` | `main` (then tagged) and `develop` |
| `hotfix/<version>` | Urgent fix to a release | `main` | `main` (then tagged) and `develop` |

`main` is the default branch on purpose: `dokku plugin:install <repository URL>` without `--committish` installs the
default branch, so it must always be a stable release. New work never starts from `main`.

- Nobody commits directly to `main` or `develop`: every change arrives through a pull request.
- Before opening or updating a pull request, rebase the branch on the latest `develop` (`git fetch` and
  `git rebase origin/develop`), so it merges without conflicts and `make check` runs on what will be merged.
- Release: in `release/<version>`, set `version` in `plugin.toml` and `__version__` in `dokku_auto_deploy/__init__.py`
  to the same value (a test checks it), merge into `main`, tag the merge commit with the bare version (`0.2.0`, no `v`
  prefix: users install with `--committish 0.2.0`), then merge `main` back into `develop`.

## Commits

- One commit, one coherent intention. Atomic is not about size: a new setting touches `settings.py`, the README
  table, tests and maybe `design-decisions.md`, and that is one commit.
- A preparatory refactor (same behavior, e.g. extracting an interface before a new implementation) goes in its own
  commit, before the feature. A bug found while refactoring is fixed in a third commit, not mixed into the refactor.
- Stage files and hunks deliberately (`git add <file>`, `git add -p`) and review the staged diff; `git add .` and
  `git add -A` pick up stray files.
- Title in English, imperative ("Add", "Fix", "Extract"), names of files, functions and commands in backticks, a plain
  hyphen (`-`) where a dash is needed. Prefer a title that stands alone; when a body is needed, keep it to a few plain
  sentences that say why (e.g. what happened before), not what the diff already shows.

## Python

- Standard library only at runtime: the plugin runs in place with the host's `python3` (3.11+), with no virtualenv and
  no package installed. Development tools (pytest, mypy, ruff, shellcheck) are the only dependencies, in the `dev`
  group of `pyproject.toml`. If something seems to need a library, discuss it first.
- `pathlib.Path` for every path, never `os.path`.
- Modern type hints everywhere (`str | None`, `list[Path]`), checked by `mypy --strict`. Avoid control flow that can
  leave a variable unbound (`possibly-undefined` is enabled).
- No one-letter names, except `_` for ignored values: `for app in apps`, not `for a in apps`.
- No comments that repeat the code. Docstrings say what a function does, its edge cases (`None`, empty, special values)
  and, when there is a non-obvious trade-off, why it was done this way. A comment is fine to explain a decision the
  code can't show (e.g. a Dokku or forge behavior).
- Errors are handled or reported, never swallowed: no `except: pass`, and `except Exception` only where every error
  must be contained (one app's failure must not stop the others) and is logged. `KeyboardInterrupt` is handled
  separately and exits with 130.
- `time.perf_counter()` for durations; `json.dumps(..., default=str)` for values that may hold `datetime`.

## External calls

- Every I/O call has an explicit timeout: HTTP through `urllib.request` (see `Forge.request`), `subprocess.run(...,
  capture_output=True, timeout=...)`. Add `text=True` only for textual output; `check=True` unless the exit code is the
  answer (like `dokku apps:locked`).
- Errors from the network become the module's own exception (`ForgeError`, `TelegramError`) with a message a person
  can act on, raised `from None` so no traceback carries a URL or header.

## Secrets

- Never pass a secret as a command-line argument (visible in `ps` and `/proc/<pid>/cmdline`), not to `dokku` nor to
  any other program: forge tokens live in the dokku user's `.netrc` (`dokku git:auth`), secret settings are read from
  stdin.
- Never log, print or put in an exception message a secret or anything that may contain one: the Telegram API URL has
  the bot token in its path, and invalid values are rejected without echoing them.
- Files holding secrets are created with mode 0600 from the start (`tempfile.NamedTemporaryFile` and `os.open(...,
  0o600)` do that), never created first and restricted later, which leaves a window where others can read them.

## Command line

The Python code only runs as `dokku auto-deploy:<command>`, so some Dokku conventions override the usual argparse
ones:

- Commands use `argparse` in `cli.py`, with `prog="dokku"` and one subparser per `auto-deploy:<command>`.
- Dokku reads `--force`, `--app`, `--quiet` and `--trace` anywhere on the command line: never use them as options.
- `--global` takes the place of an app name in `set` and `report`, and `report` accepts Dokku's
  `--auto-deploy-<name>` flags and `--format stdout|json`; `parse_args` adapts argparse to that.
- Required values are positional; options are optional by definition. Long options are kebab-case and, when a letter
  is free and unambiguous, have a short form written first (`-r`, `--redeploy`). Use `metavar` for the value's name
  and list `choices` in the help text.
- Validate and convert values in `type=` callables (see `parse_change_number`) or in `settings.KEYS`, raising
  `argparse.ArgumentTypeError`/`ConfigError` with a message that says what was expected.
- Data (reports, the build log) goes to stdout, status and errors to stderr. Silence on success is fine; `-v`
  shows more.
- Exit codes: 0 ok, 1 an app failed, 2 invalid arguments, 3 invalid settings, 130 interrupted. Keep them in the
  README when adding a case.
- Only `cli.py` prints. Other modules log through `logging.getLogger(__name__)` and return data; the build output
  reaches the terminal through a callback.
- Imports that only a command needs go inside that command's function, so `--help` stays fast.
- A new command needs a `subcommands/<name>` symlink to `default` (Dokku runs `subcommands/<name>` for
  `auto-deploy:<name>`) and a line in `help-functions`; a test checks both.

## Bash (plugin entry points and triggers)

- Follow Dokku's plugin style: `set -eo pipefail`, `[[ $DOKKU_TRACE ]] && set -x`, and functions declaring
  `desc` (and `trigger` for triggers). `nounset` is left out on purpose: Dokku's optional variables (`DOKKU_TRACE`
  and others) are unset in normal runs.
- Keep logic in Python; bash only dispatches, runs as root where needed (`install`, `update`, `uninstall`) or uses
  Dokku's own functions (`fn-plugin-property-*`).
- `make lint-check` runs shellcheck on every script; a new script goes into `SHELL_SCRIPTS` in the `Makefile`.

## Tests

- pytest with plain `assert`; no `unittest.TestCase`, `setUp` or `tearDown`. Group related tests in plain
  `class TestX:` rather than comment separators, and use `pytest.mark.parametrize` with `pytest.param(..., id=...)`
  for tables of cases.
- Test behavior through the real code paths, with the fixtures in `tests/conftest.py`: `fake_api` (a local HTTP
  server for forge APIs and Telegram that records requests and headers, can redirect or drop connections),
  `fake_dokku` (fake `dokku`, `plugn` and `systemctl` first in `PATH`) and `dokku_env` (Dokku's directories in a temp
  dir). Don't mock `urllib`, `subprocess` or the functions under test.
- Tests never touch the network, a real Dokku or the host's systemd.
- Compare whole results (the full state, the full list of requests) rather than fragments, so an extra or missing
  item fails the test.
- Write the failing test first when possible. A test added for a bug says so in its docstring ("Regression: ...").
- Before committing: `make check` (ruff check, ruff format check, shellcheck, mypy, pytest).

## Documentation

- All of the repository is in English: code, messages, docs, commits.
- The README covers installation, every setting and command, and a step-by-step setup; update it in the same commit
  as the behavior it describes. Design decisions and facts checked outside the repository (Dokku, forge or Telegram
  behavior, with where and when they were checked) go in `docs/design-decisions.md`.
- Markdown: lines up to 120 characters, straight quotes (`"`), a plain hyphen or `--` instead of an em dash, `->`
  instead of arrow characters, no `---` separators, no bold in headings, `- [ ]` for checklists.
