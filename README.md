# dokku-auto-deploy

Deploy [Dokku](https://dokku.com/) apps from GitHub branches as soon as their CI passes, without giving GitHub any
credential to your server.

It runs on the Dokku host itself: every minute it asks the GitHub API for the head of each configured branch, waits
for that commit's CI to succeed and then runs `dokku git:sync --build` with that exact commit. The result can be posted
as a comment on the pull requests that were merged and/or sent to a Telegram group.

Built for a gitflow setup where `develop` is deployed to a staging app (`stg`) and `main` to production (`prd`), but a
repository can have only one of them, and branches and app names are configurable.

Features:

- No inbound port and no secret stored on GitHub: the server pulls.
- Deploys only the exact commit whose CI succeeded; a newer push waits for its own CI. Repositories without CI can
  opt out (`workflow = ""`).
- Respects Dokku's deploy lock: never starts while another deploy (e.g. a manual `git push dokku`) runs or while the
  app is locked with `dokku apps:lock`.
- A failed deploy is reported once and not retried in a loop; `poll --force <app>` retries it.
- Notifications per repository: none, GitHub pull request comments, Telegram (group or group topic), or both. On
  failure they include the end of the build log.
- Only the Python standard library (3.11+).

The reasons behind these choices are in [`docs/design-decisions.md`](docs/design-decisions.md).

## How it works

For each target (a repository environment: `develop` -> `myproject-stg`, `main` -> `myproject-prd`), every run:

1. Reads the branch head commit (`GET /repos/{owner}/{repo}/branches/{branch}`).
2. If that commit was already handled, does nothing.
3. Looks for the runs of the configured workflow file for that commit, triggered by a push to that branch. If there is
   none yet or it is still running, waits for the next run. If it failed, records it and does nothing else. With
   `workflow = ""` this step is skipped and every new commit is deployed.
4. If the app is locked in Dokku, waits for the next run.
5. Runs `dokku git:sync --build <app> https://github.com/<owner>/<repo>.git <sha>`, streaming the build log to the
   journal.
6. Records the result and notifies the configured channels. GitHub comments go to every pull request merged into the
   branch since the last successful deploy of that app (several PRs can land in one deploy); if there is no such PR,
   there is no comment.

State is kept in a small JSON file (`/var/lib/dokku-auto-deploy/state.json` by default).

## Requirements

- A Dokku server with `git:sync` (Dokku 0.23+) and `apps:locked`.
- Python 3.11+ on the host (Debian 12+, Ubuntu 24.04+).
- A GitHub Actions workflow that runs on pushes to the deployed branches (see "GitHub Actions workflow"), unless the
  repository is configured with `workflow = ""` (no CI).

## Installation

Install it system-wide as root with [pipx](https://pipx.pypa.io/) 1.5+, which puts the command in
`/usr/local/bin/dokku-auto-deploy`:

```sh
apt install pipx                          # Debian 13 (trixie) or newer ships pipx 1.7+
pipx install --global dokku-auto-deploy
```

Debian 12 (bookworm) ships pipx 1.1, which has no `--global`. Use a newer pipx from a virtualenv:

```sh
python3 -m venv /opt/pipx-bin
/opt/pipx-bin/bin/pip install pipx
/opt/pipx-bin/bin/pipx install --global dokku-auto-deploy
```

If pipx complains about an old `uv`, add `--backend pip`. To upgrade later: `pipx upgrade --global dokku-auto-deploy`.

## Setup

All commands below run as root on the Dokku host.

### 1. GitHub token

Create a [fine-grained personal access
token](https://docs.github.com/en/authentication/keeping-your-account-secure/managing-your-personal-access-tokens),
preferably for a bot user of your organization:

- Resource owner: the organization (or user) that owns the repositories.
- Repository access: only the repositories deployed by this server.
- Permissions: `Contents: Read-only` and `Actions: Read-only` (only needed to check CI). Add
  `Pull requests: Read and write` if any repository uses the `github` notification channel (`Metadata: Read-only` is
  added automatically).

Pull request comments are posted as the user who owns the token. That is why a dedicated bot user (a regular GitHub
account created for automation, e.g. `myorg-deploy`) is better than your own account: the comments don't look like
yours, and the token doesn't depend on a person staying in the organization.

A fine-grained token covers repositories of a single owner. If this server deploys private repositories from different
owners, use a classic token or a bot user with access to all of them: Dokku keeps a single credential for
`github.com`.

Store it and give it to Dokku, which uses it to fetch private repositories:

```sh
install -d -m 700 /etc/dokku-auto-deploy
install -m 600 /dev/null /etc/dokku-auto-deploy/github-token
editor /etc/dokku-auto-deploy/github-token        # paste the token
dokku git:auth github.com <token-username> < /etc/dokku-auto-deploy/github-token
```

### 2. Dokku apps

Create one app per environment. The default names are `<repository name>-stg` and `<repository name>-prd`:

```sh
dokku apps:create myproject-stg
dokku apps:create myproject-prd
dokku git:set myproject-stg deploy-branch main
dokku git:set myproject-prd deploy-branch main
```

Setting `deploy-branch` keeps manual pushes predictable: `git:sync` with a commit SHA does not change it, and a manual
`git push dokku <branch>:main` only builds if `main` is the deploy branch. Configure the rest of each app (config vars,
databases, domains) as usual.

### 3. Configuration

Create the commented template and edit it:

```sh
dokku-auto-deploy config init            # writes /etc/dokku-auto-deploy/config.toml
editor /etc/dokku-auto-deploy/config.toml
dokku-auto-deploy config show            # validates and prints the resolved targets
```

Example:

```toml
[defaults]
workflow = ".github/workflows/django.yml"
telegram-chat = "-1001234567890_42"

[[repo]]
repository = "PythonicCafe/myproject"
notify = ["github", "telegram"]
stg = {}
prd = {}

[[repo]]
repository = "PythonicCafe/website"
name = "site"
workflow = ".github/workflows/ci.yml"
notify = ["telegram"]
telegram-chat = "-1009876543210"
prd = { app = "site-production" }
```

```console
$ dokku-auto-deploy config show
PythonicCafe/myproject develop -> myproject-stg (workflow: .github/workflows/django.yml, notify: github, telegram -1001234567890_42)
PythonicCafe/myproject main -> myproject-prd (workflow: .github/workflows/django.yml, notify: github, telegram -1001234567890_42)
PythonicCafe/website main -> site-production (workflow: .github/workflows/ci.yml, notify: telegram -1009876543210)
```

Rules: keys can be written in kebab-case (`state-file`) or snake_case (`state_file`); unknown sections or keys are
errors. `workflow` and `telegram-chat` can be set in `[defaults]`: a `[[repo]]` that doesn't set them inherits them,
and a value set in the repo wins, even an empty one. For every other key, an empty string (`""`) is the same as leaving
it out: the built-in default applies.

| Key | Where | Default | Meaning |
|---|---|---|---|
| `state-file` | `[settings]` | `/var/lib/dokku-auto-deploy/state.json` | Where the tool records what it handled |
| `github-token-file` | `[settings]` | `/etc/dokku-auto-deploy/github-token` | File with the GitHub token |
| `telegram-token-file` | `[settings]` | `/etc/dokku-auto-deploy/telegram-token` | File with the Telegram bot token (read only if a repo uses `telegram`) |
| `workflow` | `[defaults]`, `[[repo]]` | none (required in one of them) | Workflow file whose run must succeed, e.g. `.github/workflows/ci.yml`; `""` deploys every new commit without waiting for CI |
| `telegram-chat` | `[defaults]`, `[[repo]]` | none (required when `notify` has `telegram`) | Group id (`-100...`), or group id `_` topic id |
| `repository` | `[[repo]]` | required | `owner/name` on GitHub |
| `name` | `[[repo]]` | repository name | Base for app names |
| `notify` | `[[repo]]` | `[]` | Channels: `"github"`, `"telegram"`, both or none |
| `stg`, `prd` | `[[repo]]` | absent | Environment tables; each one present becomes a target. At least one is required |
| `app` | `stg`/`prd` table | `<name>-stg`, `<name>-prd` | Dokku app to deploy |
| `branch` | `stg`/`prd` table | `develop` (stg), `main` (prd) | Branch to follow |

`notify` has no default: each repository opts in.

### 4. Telegram (optional)

1. Create a bot with [@BotFather](https://t.me/BotFather) and store its token:
   ```sh
   install -m 600 /dev/null /etc/dokku-auto-deploy/telegram-token
   editor /etc/dokku-auto-deploy/telegram-token
   ```
2. Add the bot to the group (it needs permission to send messages; in a group with topics, to the chosen topic).
3. Find the chat id. In the Telegram app, copy the link of any message in the group (or topic):
   `https://t.me/c/1234567890/42/100` means group `-1001234567890`, topic `42`, so
   `telegram-chat = "-1001234567890_42"`; a link without topic (`https://t.me/c/1234567890/100`) means
   `telegram-chat = "-1001234567890"`.

Messages are HTML: the commit and pull request links sit behind the words "commit" and "#number".

### 5. First run

Run it by hand once and read the output:

```sh
dokku-auto-deploy poll
```

The first run deploys the current head of every configured branch whose CI passed. To inspect what it recorded:
`cat /var/lib/dokku-auto-deploy/state.json`.

### 6. Run it every minute (systemd)

Create the two units (also available in [`contrib/systemd/`](contrib/systemd/)):

```ini
# /etc/systemd/system/dokku-auto-deploy.service
[Unit]
Description=Deploy Dokku apps whose GitHub CI passed (dokku-auto-deploy)
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/dokku-auto-deploy poll
TimeoutStartSec=2h
```

```ini
# /etc/systemd/system/dokku-auto-deploy.timer
[Unit]
Description=Run dokku-auto-deploy every minute after the previous run finishes

[Timer]
OnBootSec=2min
OnUnitInactiveSec=1min

[Install]
WantedBy=timers.target
```

```sh
systemctl daemon-reload
systemctl enable --now dokku-auto-deploy.timer
journalctl -fu dokku-auto-deploy          # follow the logs and build output
```

`OnUnitInactiveSec` counts from the end of the previous run, so two runs never overlap: a long build only delays the
next check. `systemctl start dokku-auto-deploy` runs a check right away.

If you prefer cron, keep the same guarantees with `flock` and `timeout`:

```cron
* * * * * root flock -n /run/dokku-auto-deploy.lock timeout 2h /usr/local/bin/dokku-auto-deploy poll 2>&1 | logger -t dokku-auto-deploy
```

## Day to day

Retry a failed deploy (after fixing the cause outside the code, e.g. a config var), or put an app back on its branch
head after a manual deploy:

```sh
dokku-auto-deploy poll --force myproject-stg
```

Deploy something by hand without the tool overwriting it, e.g. test a feature branch on staging:

```sh
dokku apps:lock myproject-stg                                # optional: a merge into develop won't replace your test
git push -f dokku@server:myproject-stg feature/x:main        # from your machine
# ... test ...
dokku apps:unlock myproject-stg
dokku-auto-deploy poll --force myproject-stg                 # back to the head of develop
```

Without `apps:lock`, a manual deploy stays until the next merge into the branch. The tool never unlocks an app, and
`apps:unlock` does not stop a deploy in progress (a Dokku limitation). A manual `git push -f` doesn't break later
automatic deploys: `git:sync` with a commit SHA moves the deploy branch without requiring a fast-forward. Dokku refuses
a second deploy while one runs, so a manual `git push dokku` made during an automatic deploy fails with "currently has
a deploy lock in place"; push again when it finishes.

If the log keeps saying an app is locked while no deploy is running (a deploy killed without releasing its lock, e.g.
after a crash), release it with `dokku apps:unlock <app>`.

Failure comments include the last lines of the build log. On public repositories anyone can read them: make sure your
build does not print secrets.

## GitHub Actions workflow

For repositories that wait for CI, the tool only needs a workflow that runs on pushes to the deployed branches. With
the gitflow described here, the workflow also runs on pull requests:

```yaml
on:
  push:
    branches: [develop, main]
  pull_request:
    branches: [develop, main]
```

## Command reference

```text
dokku-auto-deploy [-c path] [-v] poll [-f app ...]    check every target once and deploy what passed CI
dokku-auto-deploy [-c path] config init [--force]     write the commented config template
dokku-auto-deploy [-c path] config show               validate the config and print the resolved targets
```

- `-c/--config`: config file (default `/etc/dokku-auto-deploy/config.toml`, or the `DOKKU_AUTO_DEPLOY_CONFIG`
  environment variable).
- `-v/--verbose`: also log targets that are waiting for CI.
- Exit codes: `0` ok; `1` at least one target failed (e.g. GitHub API error; the others still ran); `2` invalid
  arguments; `3` invalid config or missing token file; `130` interrupted.

A failed deploy or failed notification does not make the exit code non-zero: it is an expected outcome, reported
through the configured channels and the log.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
make dev-install     # dev dependencies (needs pip 25.1+ for --group)
make check           # ruff, mypy --strict and pytest
make build-check     # build and smoke-test the wheel and the sdist
```

Tests run the real code against a local fake HTTP server (GitHub and Telegram) and a fake `dokku` script; they never
touch the network or a real Dokku. See [`AGENTS.md`](AGENTS.md) for the conventions.

## License

[MIT](LICENSE), copyright (c) 2026 Pythonic Café.
