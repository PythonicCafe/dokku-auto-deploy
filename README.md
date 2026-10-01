# dokku-auto-deploy

A [Dokku](https://dokku.com/) plugin that deploys each app from a branch of its GitHub, GitLab or Forgejo repository as
soon as that commit's CI passes, without giving the forge any credential to your server.

It runs on the Dokku host itself: every minute it asks the forge API for the head of each app's branch, waits for that
commit's CI to succeed and then runs `dokku git:sync --build` with that exact commit. The result can be posted as a
comment on the pull/merge requests that were merged and/or sent to a Telegram group.

Supported forges: GitHub (github.com and GitHub Enterprise Server, with GitHub Actions), GitLab (gitlab.com and
self-managed, with GitLab CI/CD) and Forgejo (Codeberg and self-hosted, with Forgejo Actions).

Settings are per app, with a global fallback: the typical gitflow setup deploys `develop` to `myproject-stg` and `main`
to `myproject-prd`, but each app picks its own repository, branch, CI workflow and notification channels.

Features:

- No inbound port and no secret stored on the forge: the server pulls.
- Deploys only the exact commit whose CI succeeded; a newer push waits for its own CI. Waiting for CI is opt-in
  per app (`workflow`): without it, every new commit is deployed.
- Respects Dokku's deploy lock: never starts while another deploy (e.g. a manual `git push dokku`) runs or while the
  app is locked with `dokku apps:lock`.
- A failed deploy is reported once and not retried in a loop; `dokku auto-deploy:poll --redeploy <app>` retries it.
- Notifications per app: none, comments on the merged pull/merge requests, Telegram (group or group topic, a
  different one per app if you want), or both. On failure they include the end of the build log.
- Only the Python standard library (3.11+), no package to install: the plugin runs its code in place.

The reasons behind these choices are in [`docs/design-decisions.md`](docs/design-decisions.md).

## Why the server pulls

The usual way to deploy to Dokku from CI is a job on the forge (GitHub Actions, GitLab CI, Forgejo Actions) that runs
`git push dokku@server:app` with a Dokku SSH key stored in the forge's secrets. That key is far more than "permission
to deploy": whoever holds it can run any Dokku command on the server, and Dokku commands give full control of the
host. Dokku starts containers with any Docker option it is given, so these two commands, sent with that key, open a
root shell with the server's whole filesystem mounted, every app's database credentials and secrets included:

```sh
dokku docker-options:add <app> run "--user root -v /:/host"
dokku run <app> bash
```

With the key on the forge, everyone who can get it can do that:

- anyone who can change a workflow in the repository, since workflows see the secrets (a malicious or careless
  commit, a compromised developer account);
- a third-party action or CI image used by the workflow, or a compromised dependency it runs;
- the forge's administrators, and anyone who breaches the forge.

This plugin inverts the direction: the server asks the forge, through its API, which commit is at the head of a
branch and whether its CI passed, then fetches and builds that commit itself. The forge holds no credential to the
server and needs no network access to it; the server only holds a token that can read the repositories (and comment on
pull/merge requests, if enabled). The cost is latency: a deploy starts up to a minute after CI finishes. The
alternatives considered are in [`docs/design-decisions.md`](docs/design-decisions.md#pull-not-push).

## How it works

For each app with a `repository` set, every run:

1. Reads the head commit of the app's branch.
2. If that commit was already handled (deployed, or its deploy failed), does nothing.
3. Looks for the CI of that commit, triggered by a push to that branch: the runs of the configured workflow file
   (GitHub, Forgejo) or the push pipeline (GitLab); the latest one counts, so a successful retry wins. If there is none
   yet or it is still running, waits for the next run. If it failed, records it and checks again on the next runs, in
   case someone re-runs the CI. Without a `workflow` (or with `workflow none`) this step is skipped and every new commit
   is deployed.
4. If the app is locked in Dokku (a deploy in progress, or `apps:lock`), waits for the next run. If Dokku already runs
   that commit (its last successful deploy, per `dokku apps:report <app> --app-deploy-source-metadata`), only records
   it: no rebuild, no notification.
5. Runs `dokku git:sync --build <app> <repository>.git <sha>`, streaming the build log to the run's output.
6. Records the result and notifies the configured channels. Comments go to every pull/merge request merged into the
   branch since the last successful deploy of that app (several can land in one deploy); if there is none, there is no
   comment.

What was handled is shown by `dokku auto-deploy:report <app>`:

- `last sha`, `last status`, `last at`: the last branch head the plugin acted on, what happened to it (`deployed`,
  `deploy_failed` or `ci_failed`) and when. A new run only acts again when the head changes, or when the CI of a
  `ci_failed` head passes on a re-run.
- `deployed sha`: the last commit deployed successfully. It differs from the handled one after a failed deploy, and
  it bounds which merged pull/merge requests get a comment on the next successful deploy.

Losing that state is harmless: apps already running the branch head are only recorded, not rebuilt.

## Requirements

- Dokku 0.23+ (`git:sync`, `apps:locked`); 0.26+ to skip rebuilding commits an app already runs; tested on 0.38.28.
- Python 3.11+ on the host (Debian 12+, Ubuntu 24.04+). The plugin install checks it.
- A CI workflow that runs on pushes to the deployed branches (see "CI workflow"), for apps with a
  `workflow`. On Forgejo, waiting for CI needs Forgejo 12+ (the version that added the Actions runs API).

## Installation

Install a released version (the tags are listed in the repository's releases), as root:

```sh
dokku plugin:install https://github.com/PythonicCafe/dokku-auto-deploy.git --committish 0.1.0
```

Without `--committish`, Dokku installs the repository's default branch, which may have unreleased changes: prefer a
tag. To upgrade later, or go back to a previous version:

```sh
dokku plugin:update auto-deploy 0.2.0
```

The plugin is named `auto-deploy` (Dokku drops the `dokku-` prefix of the repository name), so its commands are
`dokku auto-deploy:*`. Installing and updating create systemd units that can run it every minute, disabled; choosing
between them and cron is part of the setup below.

`dokku plugin:uninstall auto-deploy` stops scheduling runs, waits for a run in progress to finish (it may be deploying
an app), then removes the systemd units. Settings and state stay, so reinstalling picks them up.

## Setup

All commands below run on the Dokku host, as root or as a user allowed to run `dokku`.

### 1. Token

One token per forge host does both jobs: Dokku uses it to clone the repository (`git:sync` over HTTPS, private
repositories included) and the plugin uses it for the API calls (branch head, CI, merged changes, comments). It is the
credential `dokku git:auth` stores for the repository host, in the dokku user's `.netrc`, and it works the same way on
GitHub, GitLab and Forgejo. Give it through a pipe:

```sh
cat token-file | dokku git:auth <host> <token-username>      # e.g. github.com, gitlab.com, codeberg.org
rm token-file
```

The token must come through a pipe: Dokku only reads it from standard input when stdin is a pipe
(`[[ -p /dev/stdin ]]`, checked in v0.38.28), so a `< file` redirection fails with "Missing password". Avoid passing
it as an argument, which shows it in the process list. A token is needed even for public repositories: the plugin
calls the API every minute and unauthenticated calls are rate-limited (60 per hour on GitHub).

Dokku keeps one credential per host, so one token serves every app from that host: the host in the app's `repository`
URL picks it, and `dokku auto-deploy:report <app> --auto-deploy-forge-login` shows which account it belongs to.
Comments are posted as the token's owner, so a bot account is better than your own: the comments don't look like yours,
and the token doesn't depend on a person staying in the organization.

Repositories of several owners (organizations or users) on the same host share that one token, so it must reach all of
them. Use one bot account that is a member of every owner, with read access to the deployed repositories (read access
is enough to comment on pull/merge requests), and a token that isn't bound to a single owner:

- GitHub: a classic personal access token with the `repo` scope. Fine-grained tokens are bound to one resource owner.
  The `repo` scope allows writing, so limit what the bot can do through its role in each repository (Read).
- GitLab: a personal access token of the bot user (group and project access tokens are bound to their group or
  project).
- Forgejo: the bot user's access token already covers every repository the bot can read.

Separate tokens per project on the same host aren't supported: the clone would need its own credential too, and
putting it in the `git:sync` URL would show it in the process list.

#### GitHub

Create a [fine-grained personal access
token](https://docs.github.com/en/authentication/keeping-your-account-secure/managing-your-personal-access-tokens),
preferably for a bot user of your organization:

- Resource owner: the organization (or user) that owns the repositories.
- Repository access: only the repositories deployed by this server.
- Permissions: `Contents: Read-only`, `Actions: Read-only` (to check CI) and `Pull requests: Read-only` (to list the
  merged pull requests in notifications); `Pull requests: Read and write` if any app uses the `comment` channel.
  `Metadata: Read-only` is added automatically.

The bot user is a regular GitHub account created for automation (e.g. `myorg-deploy`); `<token-username>` is its
login. A fine-grained token covers repositories of a single owner: if this server deploys private repositories from
different owners, use a classic token or a bot user with access to all of them.

#### GitLab

Create a [group access token](https://docs.gitlab.com/user/group/settings/group_access_tokens/) (or a project access
token, for a single project) with role Reporter and scopes `read_api` and `read_repository`; with the `comment`
channel, use scope `api` instead of `read_api`, as posting a note needs it. GitLab creates a bot user for the token,
and comments appear as that bot. Personal access tokens work too, with the same scopes. Any non-empty
`<token-username>` works for Git over HTTPS with a token.

#### Forgejo

Create an access token (user settings, Applications) for a bot user with access to the repositories, with scope
`read:repository`, plus `write:issue` for the `comment` channel (comments on pull requests go through the issues API).
`<token-username>` is the bot user's login.

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
dokku auto-deploy:set site-prd workflow none                          # no CI: overrides the global workflow
dokku auto-deploy:set site-prd notify telegram

dokku auto-deploy:report                                               # check everything
```

`dokku auto-deploy:set <app>|--global <key>` without a value unsets the key.

`report` follows Dokku's report conventions. For each key that can be set in both places it shows the app's own value
(`Auto deploy workflow`), the global one (`Auto deploy global workflow`) and the one in effect, defaults included
(`Auto deploy computed workflow`). It also shows the login of the token used for the repository host
(`Auto deploy forge login`, from `dokku git:auth`), a `problem` line when the app's settings are incomplete, and the
last handled and deployed commits:

```sh
dokku auto-deploy:report myproject-stg                                   # one app
dokku auto-deploy:report myproject-stg --format json
dokku auto-deploy:report myproject-stg --auto-deploy-computed-workflow   # only that value
dokku auto-deploy:report --global                                        # global settings and schedule
dokku auto-deploy:report --format json                                   # every app, one object keyed by app name
```

| Key | Scope | Meaning |
|---|---|---|
| `repository` | app | Repository web URL (`https://github.com/owner/name`, `https://gitlab.com/group/subgroup/project`, `https://codeberg.org/owner/name`). Setting it enables auto-deploy for the app; unsetting it disables it |
| `branch` | app | Branch to deploy (required) |
| `forge` | app | Forge type, only for hosts other than github.com, gitlab.com and codeberg.org: `github` (GitHub Enterprise Server), `gitlab` (self-managed GitLab) or `forgejo` |
| `workflow` | app, global | GitHub, Forgejo: workflow file whose run must succeed, e.g. `.github/workflows/ci.yml` or `.forgejo/workflows/ci.yml` (Forgejo only looks at the file name). GitLab: any value but `none` waits for the push pipeline (e.g. `.gitlab-ci.yml`). `none` (default) deploys every new commit without waiting for CI |
| `notify` | app, global | Comma-separated channels: `comment`, `telegram`, both, or `none` (default) |
| `telegram-chat` | app, global | Group id (`-100...`), or group id `_` topic id (required when `notify` has `telegram`) |
| `telegram-bot-token` | global | Telegram bot token, read from stdin |

Self-hosted forges: set `forge` for any host other than github.com, gitlab.com and codeberg.org (when it's unset, the
plugin only recognizes those three hosts). Every URL is then built from the repository URL's scheme, host and port:
the API (`/api/v3` for GitHub Enterprise Server, `/api/v4` for GitLab, `/api/v1` for Forgejo), the clone URL and the
commit links. Only github.com has its API elsewhere (`api.github.com`). Links to pull/merge requests come from the
forge's own API answers, so they use the URL the forge is configured with. A forge installed under a path
(`https://example.com/gitlab/...`) isn't supported.

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
   `--pull-request`, it also comments on that pull request (merge request on GitLab) of the app's repository.

Messages are HTML: the commit link sits behind the word "commit", each pull/merge request link spans its reference and
title ("#12 Title" on GitHub and Forgejo, "!12 Title" on GitLab), and the app URL (from `dokku url <app>`) is shown in
full. Comments link the commit and show the app URL too.

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
2. Wait for the CI run of the merge commit to finish (skip this without a `workflow`).
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

Choose one of two schedulers. Either way runs never overlap: a `poll` started while another one runs (e.g. a manual
one) exits right away.

The systemd timer (installed with the plugin, disabled) logs to the journal. Enable it as root:

```sh
dokku auto-deploy:schedule systemd        # makes sure cron is off, then prints the command below
systemctl enable --now dokku-auto-deploy.timer
journalctl -fu dokku-auto-deploy          # follow the logs and build output
```

The service runs `dokku auto-deploy:poll` as the dokku user. `OnUnitInactiveSec` counts from the end of the previous
run, so a long build only delays the next check; `systemctl start dokku-auto-deploy` runs a check right away.

Or cron, fully managed by the plugin and without root: it adds a task to the crontab Dokku manages for the dokku user
(`dokku cron:list --global` shows it), logging to `/var/log/dokku/auto-deploy.log` (rotated with Dokku's other logs):

```sh
dokku auto-deploy:schedule cron
tail -f /var/log/dokku/auto-deploy.log
```

Dokku only writes this task when at least one app uses a scheduler that runs on the host crontab (`docker-local`, the
default): with every app on k3s, for instance, `schedule cron` finds no task in the crontab, undoes the change and says
to use the systemd timer.

`dokku auto-deploy:schedule` shows the current choice, and `dokku auto-deploy:schedule none` removes the cron task
(the timer, if enabled, needs `systemctl disable --now dokku-auto-deploy.timer`, as the command reminds you).

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

For apps that wait for CI, the plugin only needs CI that runs on pushes to the deployed branches. With the gitflow
described here, it also runs on pull requests. On GitHub Actions:

```yaml
on:
  push:
    branches: [develop, main]
  pull_request:
    branches: [develop, main]
```

Forgejo Actions uses the same syntax, from `.forgejo/workflows/` (or `.github/workflows/`). On GitLab, the pipeline of
a push to the branch counts (`source` `push`; merge request pipelines don't). A pipeline
waiting for a manual job counts as still running, so the deploy waits for it.

## Command reference

```text
dokku auto-deploy:set <app>|--global <key> [<value>]      set a setting, or unset it when no value is given
dokku auto-deploy:report [<app>|--global] [--format stdout|json] [--auto-deploy-<name>]
                                                          show settings and the last handled commit
dokku auto-deploy:poll [--redeploy <app>] [--verbose]     deploy every configured app whose branch head passed CI
dokku auto-deploy:notify-test <app> [--pull-request <n>]  send a test Telegram message and optionally a test comment
dokku auto-deploy:schedule [systemd|cron|none]           show or choose what runs poll every minute
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
match), merge into `main` and tag it without a `v` prefix (`git tag 0.2.0 && git push --tags`).

## License

[MIT](LICENSE), copyright (c) 2026 Pythonic Café.
