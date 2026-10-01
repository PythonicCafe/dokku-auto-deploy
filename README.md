# dokku-auto-deploy

A [Dokku](https://dokku.com/) plugin that deploys each app from a branch of its GitHub repository as soon as that
commit's CI passes, without giving GitHub any credential to your server.

It runs on the Dokku host itself: every minute it asks the GitHub API for the head of each app's branch, waits for
that commit's CI to succeed and then runs `dokku git:sync --build` with that exact commit. The result can be posted as
a comment on the pull requests that were merged and/or sent to a Telegram group.

Settings are per app, with a global fallback: the typical gitflow setup deploys `develop` to `myproject-stg` and `main`
to `myproject-prd`, but each app picks its own repository, branch, CI workflow and notification channels.

Features:

- No inbound port and no secret stored on GitHub: the server pulls.
- Deploys only the exact commit whose CI succeeded; a newer push waits for its own CI. Apps without CI can opt out
  (`workflow none`).
- Respects Dokku's deploy lock: never starts while another deploy (e.g. a manual `git push dokku`) runs or while the
  app is locked with `dokku apps:lock`.
- A failed deploy is reported once and not retried in a loop; `dokku auto-deploy:poll --redeploy <app>` retries it.
- Notifications per app: none, comments on the merged pull requests, Telegram (group or group topic, a different one
  per app if you want), or both. On failure they include the end of the build log.
- Only the Python standard library (3.11+), no package to install: the plugin runs its code in place.

The reasons behind these choices are in [`docs/design-decisions.md`](docs/design-decisions.md).

## How it works

For each app with a `repository` set, every run:

1. Reads the head commit of the app's branch.
2. If that commit was already handled, does nothing.
3. Looks for the runs of the configured workflow file for that commit, triggered by a push to that branch. If there is
   none yet or it is still running, waits for the next run. If it failed, records it and does nothing else. With
   `workflow none` this step is skipped and every new commit is deployed.
4. If Dokku already runs that commit (its last successful deploy, per `dokku apps:report <app>
   --app-deploy-source-metadata`), only records it: no rebuild, no notification. If the app is locked in Dokku, waits
   for the next run.
5. Runs `dokku git:sync --build <app> <repository>.git <sha>`, streaming the build log to the run's output.
6. Records the result and notifies the configured channels. Comments go to every pull request merged into the branch
   since the last successful deploy of that app (several can land in one deploy); if there is none, there is no
   comment.

What was handled is shown by `dokku auto-deploy:report <app>`:

- `last handled`: the last branch head the plugin acted on, what happened to it (`deployed`, `deploy_failed` or
  `ci_failed`) and when. A new run only acts again when the head changes.
- `last deployed`: the last commit deployed successfully. It differs from the handled one after a failed deploy, and
  it bounds which merged pull requests get a comment on the next successful deploy.

Losing that state is harmless: apps already running the branch head are only recorded, not rebuilt.

## Requirements

- Dokku 0.23+ (`git:sync`, `apps:locked`); 0.26+ to skip rebuilding commits an app already runs; tested on 0.38.28.
- Python 3.11+ on the host (Debian 12+, Ubuntu 24.04+). The plugin install checks it.
- A CI workflow that runs on pushes to the deployed branches (see "CI workflow"), unless the app uses
  `workflow none`.

## Installation

Install a released version (the tags are listed in the repository's releases), as root:

```sh
dokku plugin:install https://github.com/PythonicCafe/dokku-auto-deploy.git --committish v0.1.0
```

Without `--committish`, Dokku installs the repository's default branch, which may have unreleased changes: prefer a
tag. To upgrade later, or go back to a previous version:

```sh
dokku plugin:update auto-deploy v0.2.0
```

The plugin is named `auto-deploy` (Dokku drops the `dokku-` prefix of the repository name), so its commands are
`dokku auto-deploy:*`. Installing and updating create the systemd units that run it every minute, disabled; enabling
them is part of the setup below.

## Setup

All commands below run on the Dokku host, as root or as a user allowed to run `dokku`.

### 1. Token

Create a [fine-grained personal access
token](https://docs.github.com/en/authentication/keeping-your-account-secure/managing-your-personal-access-tokens),
preferably for a bot user of your organization:

- Resource owner: the organization (or user) that owns the repositories.
- Repository access: only the repositories deployed by this server.
- Permissions: `Contents: Read-only`, `Actions: Read-only` (to check CI) and `Pull requests: Read-only` (to list the
  merged pull requests in notifications); `Pull requests: Read and write` if any app uses the `comment` channel.
  `Metadata: Read-only` is added automatically.

Comments are posted as the user who owns the token. That is why a dedicated bot user (a regular GitHub account created
for automation, e.g. `myorg-deploy`) is better than your own account: the comments don't look like yours, and the
token doesn't depend on a person staying in the organization.

Give it to Dokku with `git:auth`. Dokku uses it to fetch private repositories, and the plugin reads the same entry
(the `.netrc` of the dokku user) for the API:

```sh
cat token-file | dokku git:auth github.com <token-username>
rm token-file
```

`<token-username>` is the login of the account that owns the token (e.g. the bot user). The token must come through a
pipe: Dokku only reads it from standard input when stdin is a pipe (`[[ -p /dev/stdin ]]`, checked in v0.38.28), so a
`< file` redirection fails with "Missing password". Avoid passing it as an argument, which shows it in the process
list. A token is needed even for public repositories: unauthenticated API calls are limited to 60 per hour.

Dokku keeps one credential per host, so one token serves every app from that host. A fine-grained token covers
repositories of a single owner: if this server deploys private repositories from different owners, use a classic
token or a bot user with access to all of them.

### 2. Apps

Create the apps as usual (config vars, databases, domains) and set their deploy branch:

```sh
dokku apps:create myproject-stg
dokku git:set myproject-stg deploy-branch main
```

Setting `deploy-branch` keeps manual pushes predictable: `git:sync` with a commit SHA does not change it, and a manual
`git push dokku <branch>:main` only builds if `main` is the deploy branch.

### 3. Settings

Each app is enabled by setting its `repository`; the other keys can be set per app or globally with `--global` (the
app's value wins). Values that are the same for every app go well in the global settings:

```sh
dokku auto-deploy:set --global workflow .github/workflows/ci.yml
dokku auto-deploy:set --global notify comment,telegram
dokku auto-deploy:set --global telegram-chat -1001234567890_42

dokku auto-deploy:set myproject-stg repository https://github.com/PythonicCafe/myproject
dokku auto-deploy:set myproject-stg branch develop
dokku auto-deploy:set myproject-prd repository https://github.com/PythonicCafe/myproject
dokku auto-deploy:set myproject-prd branch main
dokku auto-deploy:set myproject-prd telegram-chat -1001234567890_7    # production goes to another topic

dokku auto-deploy:set site-prd repository https://github.com/PythonicCafe/website
dokku auto-deploy:set site-prd branch main
dokku auto-deploy:set site-prd workflow none                          # no CI: deploy every new commit
dokku auto-deploy:set site-prd notify telegram

dokku auto-deploy:report                                               # check everything
```

`dokku auto-deploy:set <app>|--global <key>` without a value unsets the key. `report` shows where each value comes
from (`(global)` when inherited) and a `problem:` line if the app's settings are incomplete.

| Key | Scope | Meaning |
|---|---|---|
| `repository` | app | Repository web URL (`https://github.com/owner/name`). Setting it enables auto-deploy for the app; unsetting it disables it |
| `branch` | app | Branch to deploy (required) |
| `forge` | app | Forge type, only for hosts other than github.com: `github` for GitHub Enterprise Server |
| `workflow` | app, global | Workflow file whose run must succeed, e.g. `.github/workflows/ci.yml`; `none` deploys every new commit without waiting for CI (required) |
| `notify` | app, global | Comma-separated channels: `comment`, `telegram`, both, or `none` (default: none) |
| `telegram-chat` | app, global | Group id (`-100...`), or group id `_` topic id (required when `notify` has `telegram`) |
| `telegram-bot-token` | global | Telegram bot token, read from stdin |

Settings live in Dokku's property store (`/var/lib/dokku/config/auto-deploy/`); deleting or renaming an app deletes or
moves its settings too.

### 4. Telegram (optional)

1. Create a bot with [@BotFather](https://t.me/BotFather) and give its token to the plugin through stdin (never as an
   argument):
   ```sh
   cat bot-token-file | dokku auto-deploy:set --global telegram-bot-token
   rm bot-token-file
   ```
   `< bot-token-file` works too. Running it from a terminal without input unsets it; empty input is an error.
2. Add the bot to the group (it needs permission to send messages; in a group with topics, to the chosen topic).
3. Find the chat id. In the Telegram app, copy the link of any message in the group (or topic):
   `https://t.me/c/1234567890/42/100` means group `-1001234567890`, topic `42`, so the chat is `-1001234567890_42`; a
   link without topic (`https://t.me/c/1234567890/100`) means `-1001234567890`.
4. Check it end to end:
   ```sh
   dokku auto-deploy:notify-test myproject-stg
   dokku auto-deploy:notify-test myproject-stg --pull-request 12    # also comment on pull request #12
   ```
   It sends a test message to the app's chat and prints `ok telegram <chat>` or the Telegram error; with
   `--pull-request`, it also comments on that pull request of the app's repository.

Messages are HTML: the commit link sits behind the word "commit", each pull request link spans "#number title", and the
app URL (from `dokku url <app>`) is shown in full. Comments link the commit and show the app URL too.

### 5. First run

Run it by hand once and read the output:

```sh
dokku auto-deploy:poll
```

The first run deploys the current head of every app's branch whose CI passed, except in apps that already run that
commit (they are only recorded). `dokku auto-deploy:report` shows what was recorded.

### 6. Try the whole flow

Before scheduling it, follow one change end to end, running `poll` by hand (`-v` also shows apps waiting for CI):

1. On your machine, create a branch from `develop`, make a small visible change, push it and open a pull request into
   `develop`. Merge it (merge, squash or rebase: all work).
2. Wait for the CI run of the merge commit to finish (skip this with `workflow none`).
3. On the server:
   ```sh
   dokku auto-deploy:poll -v
   ```
   The log shows `deploying develop@<sha>`, the build output, `<sha>: deployed` and one line per notification.
4. Check the result:
   - [ ] The change is live at the staging app's URL.
   - [ ] The pull request got a comment linking the commit and the app URL (with `comment` in `notify`).
   - [ ] The Telegram chat got a message (with `telegram` in `notify`).
   - [ ] Running `dokku auto-deploy:poll -v` again does nothing: the commit was already handled.

Promoting to production is the same flow with a pull request from `develop` into `main`.

### 7. Run it every minute

The plugin installed a systemd service and timer; enable the timer as root:

```sh
systemctl enable --now dokku-auto-deploy.timer
journalctl -fu dokku-auto-deploy          # follow the logs and build output
```

The service runs `dokku auto-deploy:poll` as the dokku user. `OnUnitInactiveSec` counts from the end of the previous
run, so a long build only delays the next check; `systemctl start dokku-auto-deploy` runs a check right away. Runs
never overlap anyway: a `poll` started while another one runs (e.g. a manual one) exits right away.

## Day to day

Retry a failed deploy (after fixing the cause outside the code, e.g. a config var), or put an app back on its branch
head after a manual deploy:

```sh
dokku auto-deploy:poll --redeploy myproject-stg
```

Deploy something by hand without the plugin overwriting it, e.g. test a feature branch on staging:

```sh
dokku apps:lock myproject-stg                                # optional: a merge into develop won't replace your test
git push -f dokku@server:myproject-stg feature/x:main        # from your machine
# ... test ...
dokku apps:unlock myproject-stg
dokku auto-deploy:poll --redeploy myproject-stg              # back to the head of develop
```

Without `apps:lock`, a manual deploy stays until the next merge into the branch. The plugin never unlocks an app, and
`apps:unlock` does not stop a deploy in progress (a Dokku limitation). A manual `git push -f` doesn't break later
automatic deploys: `git:sync` with a commit SHA moves the deploy branch without requiring a fast-forward. Dokku refuses
a second deploy while one runs, so a manual `git push dokku` made during an automatic deploy fails with "currently has
a deploy lock in place"; push again when it finishes.

If the log keeps saying an app is locked while no deploy is running (a deploy killed without releasing its lock, e.g.
after a crash), release it with `dokku apps:unlock <app>`.

To stop deploying an app automatically: `dokku auto-deploy:set <app> repository` (unset). Failure comments include the
last lines of the build log; on public repositories anyone can read them, so make sure your build does not print
secrets.

## CI workflow

For apps that wait for CI, the plugin only needs a workflow that runs on pushes to the deployed branches. With the
gitflow described here, the workflow also runs on pull requests:

```yaml
on:
  push:
    branches: [develop, main]
  pull_request:
    branches: [develop, main]
```

## Command reference

```text
dokku auto-deploy:set <app>|--global <key> [<value>]      set a setting, or unset it when no value is given
dokku auto-deploy:report [<app>|--global]                 show settings and the last handled commit
dokku auto-deploy:poll [--redeploy <app>] [--verbose]     deploy every configured app whose branch head passed CI
dokku auto-deploy:notify-test <app> [--pull-request <n>]  send a test Telegram message and optionally a test comment
```

- Every command takes `--help`, and `dokku auto-deploy:help` lists them.
- `poll -r/--redeploy <app>` (repeatable): deploy the app's branch head even if it was already handled or already
  runs. It is not called `--force` because Dokku takes `--force` for itself.
- `poll -v/--verbose`: also log apps that are waiting for CI.
- Exit codes: `0` ok; `1` at least one app failed (e.g. API error; the others still ran); `2` invalid arguments; `3`
  invalid or incomplete settings; `130` interrupted.

A failed deploy or failed notification does not make the exit code non-zero: it is an expected outcome, reported
through the configured channels and the log.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
make dev-install     # dev dependencies (needs pip 25.1+ for --group)
make check           # ruff, shellcheck, mypy --strict and pytest
```

Tests run the real code, and the plugin's bash entry points, against a local fake HTTP server (forge and Telegram) and
a fake `dokku` script; they never touch the network or a real Dokku. See [`AGENTS.md`](AGENTS.md) for the conventions.

Releasing: bump `version` in `plugin.toml` and `__version__` in `dokku_auto_deploy/__init__.py` (a test checks they
match), merge into `main` and tag it (`git tag v0.2.0 && git push --tags`).

## License

[MIT](LICENSE), copyright (c) 2026 Pythonic Café.
