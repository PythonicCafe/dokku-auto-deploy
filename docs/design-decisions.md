# Design decisions

Why `dokku-auto-deploy` works the way it does. Implementation details that are obvious from the code live in
docstrings; this file keeps the reasoning and the facts checked outside this repository.

## Pull, not push

Requirement: GitHub must not hold any credential that gives access to the Dokku server.

| Option | Credential on GitHub | Exposed port | Latency |
|---|---|---|---|
| Actions job runs `git push dokku` over SSH (most common) | Dokku SSH key | SSH | seconds |
| Self-hosted runner on the server | none, but anyone with write access can add a workflow that runs there | no | seconds |
| Webhook -> receiver on the server | HMAC secret (no server access) | HTTPS | seconds |
| Polling the GitHub API (chosen) | none | no | up to ~1 min + CI |

A future webhook receiver should only trigger a polling cycle (the decision keeps querying the API), so a leaked webhook
secret can make the tool check earlier but never deploy something arbitrary.

The tool calls the local `dokku` binary: it runs on the Dokku host, so there is no SSH key to leak.

## A Dokku plugin, not a Python package

It started as a PyPI package installed with pipx and configured with a TOML file. As a plugin:

- Install and upgrade are one Dokku command each (`plugin:install --committish <tag>`, `plugin:update auto-deploy
  <tag>`), with no pip, pipx or virtualenv; the code is stdlib-only, so it runs in place from the plugin directory
  (`subcommands/default` runs `dokku_auto_deploy.cli.main` with `python3 -I`, so neither `PYTHON*` variables nor
  the caller's current directory can change which code runs as the dokku user).
- Settings are per app, in Dokku's property store, next to the app's other settings, with a global fallback (e.g.
  a Telegram chat per app, or one for all). Triggers keep them in sync: `post-delete` removes them,
  `post-app-rename-setup` moves them. A TOML file listing repositories and environments had to be kept in sync with
  the apps by hand.
- Commands follow Dokku's conventions (`auto-deploy:set <app>|--global <key> [<value>]`, `auto-deploy:report`).

Facts checked in Dokku 0.38.28's source (2026-09) that shaped it:

- Plugin commands run as the dokku user: the `dokku` script re-executes itself with `sudo -u dokku` for every command
  except `plugin:*`, `ssh-keys:add/remove` and two `scheduler-k3s` ones (a hardcoded list). A command can't write
  systemd units; the `install` trigger, which runs as root during `plugin:install`, can. `plugin:update` runs the
  `update` trigger, not `install`, so `update` runs the same (idempotent) script.
- `plugin:install` names the plugin after the repository, dropping a `dokku-` prefix: `dokku-auto-deploy` becomes
  `auto-deploy`. Without `--committish` it installs the default branch.
- Dokku's own argument parsing (`parse_args`) looks for `--force`, `--app`, `--quiet` and `--trace` anywhere on the
  command line and sets its own variables (e.g. `--force` sets `DOKKU_APPS_FORCE_DELETE`); it only removes them when
  they come before the command. So `poll` retries with `--redeploy`, not `--force`.
- For a community plugin, `dokku auto-deploy:<name>` runs `subcommands/<name>` and a bare `dokku auto-deploy` runs
  `subcommands/default` (`execute_dokku_cmd`); a command without its script falls through to the `commands` files and
  fails as "not a dokku command". Every `subcommands/<name>` is a symlink to `default`, which gets the full command
  name as `$1`. `commands` is what `dokku help` calls.
- Properties are plain files, `<DOKKU_LIB_ROOT>/config/<plugin>/<app>/<key>`, mode 0600, `--global` stored as an
  app of that name (`plugins/common/properties.go`). The Python code reads and writes that layout directly, so the
  bash triggers can use Dokku's `fn-plugin-property-*` functions on the same data.

## Scheduling: systemd timer or Dokku's cron

Alternatives: plain cron (needs log redirection and something against overlapping runs), a `while true; sleep` daemon
(state in memory, no way to trigger a run by hand) and an app inside Dokku (would need a Dokku SSH key inside a
container). With `OnUnitInactiveSec`, the interval counts from the end of the previous run: a long build only delays
the next check. Timeout (`TimeoutStartSec`), logs (`journalctl -u`) and manual runs (`systemctl start`) come for free.
The service runs as the dokku user (`User=`), like any plugin command.

`TimeoutStartSec=infinity`: each deploy already has a 1h timeout, and systemd killing `poll` in the middle of a build
(several apps deploying in one run can take longer than any fixed limit) would leave the app's deploy lock file behind.

The `install` trigger writes the units but leaves the timer disabled: enabling it is the admin's decision, and
`plugin:update` rewrites the units without changing whether the timer is enabled. A plugin command can't enable it
(it runs as the dokku user), so `auto-deploy:schedule systemd` prints the `systemctl` command instead.

Cron is offered too (`auto-deploy:schedule cron`), through Dokku's own mechanism rather than `/etc/cron.d`: Dokku's
cron plugin regenerates the whole dokku user crontab (`crontab -u dokku`) on every deploy and `cron:*` change, so a line
added by hand would be lost; plugins add tasks through the `cron-entries` trigger instead (`$SCHEDULE;$COMMAND;$LOG`,
the log appended with `&>>`), and `plugn trigger scheduler-cron-write` regenerates the crontab on demand, all as the
dokku user (checked in `plugins/cron/crontab.go`, 0.38.28). The trigger is called once per scheduler that uses the host
crontab, so it only answers for `docker-local`. Found there too: an empty line in a trigger's output makes Dokku drop
every task that trigger returned, so it prints exactly one line. The trigger is only asked for the schedulers that
some app uses: with no app on `docker-local` (all on k3s, say), the task never reaches the crontab, so `schedule cron`
reads the crontab back and undoes itself when the task is missing. `poll` also takes a non-blocking
`flock` on `<DOKKU_LIB_ROOT>/data/auto-deploy/poll.lock` and exits if another `poll` holds it, so a manual run during
a scheduled one (or two schedulers) never deploys twice.

## Deploying the exact commit

`dokku git:sync --build <app> <url> <sha>` receives the SHA whose CI was checked, so a push that arrives during the
check waits for its own CI. With an explicit SHA, `git:sync` moves the deploy branch with `update-ref`, without
requiring a fast-forward (checked in Dokku's `plugins/git/internal-functions`, 2026-09): a manual `git push -f` to the
app doesn't break the next automatic deploy. Side effect: `git:sync` with a SHA never changes the app's
`deploy-branch`, hence the README's `dokku git:set <app> deploy-branch main`.

Which CI run counts is up to each forge (next section); `workflow none` skips this check entirely (repositories without
CI).

## Forges

Each forge implements the same small interface (`forge.Forge`): branch head, CI status of a commit, commits between
two SHAs, recently merged changes, comment, commit URL. The forge comes from the repository host (github.com,
gitlab.com, codeberg.org) or the app's `forge` setting.

What "CI passed" means, per forge (the latest run counts, so a successful retry replaces a failure):

- GitHub: runs of the configured workflow file (`path`) for that SHA, triggered by a push to that branch. The API
  can't filter by file, so it is filtered after fetching.
- GitLab: the project's pipeline for that SHA with `source=push` on that branch (one pipeline per push, defined by the
  project's CI configuration), so `workflow` only turns the wait on or off. Statuses checked in GitLab's API docs
  (2026-09): `success` passes; `failed`, `canceled` and `skipped` fail; everything else (`created`, `pending`,
  `running`, `manual`, `scheduled`, ...) waits. A pipeline blocked on a manual job therefore waits until someone runs
  it.
- Forgejo: the Actions runs API, filtered by SHA, `event=push`, `ref=refs/heads/<branch>` and `workflow_id` (the
  workflow's file name, e.g. `ci.yml`, whatever its directory). The runs endpoint exists since Forgejo 12 with
  `head_sha` and `event` filters; `ref` and `workflow_id` only came in 15, and earlier versions ignore unknown
  parameters, so the answer is filtered again by `workflow_id` and `prettyref` (the branch) (checked in Forgejo's
  source, tags v12.0.0 to v15.0.0). Checked on Codeberg (Forgejo 16.0.0-dev, 2026-09-29), anonymously, on
  `forgejo/docs`: `ref` needs the full `refs/heads/<branch>` (a bare branch name matches nothing) and `workflow_id` the
  bare file name (a path matches nothing); runs have `workflow_id` `cli.yml`, `prettyref` `next`, `event` `push`.
  Statuses: `success` passes; `failure`, `cancelled` and `skipped` fail; `waiting`, `running`, `blocked` and `unknown`
  wait. Older Forgejo versions can't wait for CI (`workflow none` still works with them).

How a merged change is recognized in the deployed range: GitHub's `merge_commit_sha` is the merge commit, the squash
commit or the last rebased commit. GitLab has `merge_commit_sha` (null for fast-forward merges) and `squash_commit_sha`;
when both are null (a fast-forward merge), the MR head `sha` is the commit that landed on the branch. The head `sha` is
not used otherwise: it can reach the branch through another MR built on top of this one, and this MR would then be
notified for a deploy that isn't its own.
Forgejo's `merge_commit_sha` works like GitHub's; its pull request list can't filter by base branch, so that's done
after fetching (50 per page, Forgejo's default maximum).

## Dokku's deploy lock: wait, never unlock

Checked in Dokku's source (2026-09): `apps:locked` only tests whether the app's `.deploy.lock` file exists. A git
deploy (`receive-app`, used by both `git push` and `git:sync --build`) creates that file under `flock` in `exclusive`
mode, fails immediately (it does not wait) if the lock is taken, and deletes the file when done. Consequences:

- The tool checks `apps:locked` before deploying and waits while the app is locked (a deploy in progress, from anyone,
  or a manual `apps:lock`).
- A manual `git push dokku` during an automatic deploy fails with "currently has a deploy lock", and vice versa. If
  `git:sync` fails and the app is locked right after, the tool treats it as a lost race: no state change, no
  notification, retry next cycle.
- In `git:sync`, fetch and `update-ref` happen before the lock: after a lost race, the app's repo points to the new SHA
  while the running container is still the manual deploy.
- A deploy killed without releasing the lock (`kill -9`, crash) leaves the file behind and the tool waits forever,
  logging it every cycle; `dokku apps:unlock <app>` fixes it. The tool never unlocks on its own: other tools we looked
  at do, which silently overrides manual deploys.

## Commits Dokku already runs are not rebuilt

Before deploying, the tool asks Dokku what the app's last successful deploy was: `dokku apps:report <app>
--app-deploy-source-metadata`. If that is the branch head, the commit is only recorded as deployed. Without this, the
first run (or a lost state) rebuilt every app, production included, even when nothing changed.

Dokku writes `deploy-source-metadata` in the `deploy-source-set` trigger, which only runs after a deploy succeeded:
`<sha>` for a `git push`, `<remote>#<sha>` for `git:sync` (since Dokku 0.26.0, #4862). Checked on a Dokku 0.38.28
server (2026-09-30) with a test app: a build that fails (`RUN false` in the Dockerfile) and a build whose container
fails the checks both left the metadata at the previous commit, through `git push` and through `git:sync`.

`GIT_REV` (`dokku config:get <app> GIT_REV`) was used before and dropped: Dokku sets it before building, so in the
same test it named each commit whose deploy had just failed. A failed manual deploy of the branch head would then have
been recorded as deployed. `git:report --git-sha` doesn't help either: it runs `git rev-parse HEAD` in the app's bare
repo, which printed the literal string `HEAD` on that server.

The lock is checked first, so a deploy of the branch head still in progress is waited for. `poll --redeploy <app>`
rebuilds regardless.

## Failed deploys are not retried automatically

A SHA whose deploy failed is recorded and left alone, so a broken commit isn't rebuilt every minute. A SHA whose CI
failed is different: nothing was built, and re-running a flaky CI is the usual fix, so its CI keeps being checked (one
API call per cycle) and a successful re-run deploys it. Retrying is
explicit (`poll --redeploy <app>`), which also puts an app back on its branch head after a manual deploy.

## State and notified changes

The state (a JSON `state` property per app, written to a temp file and renamed, saved as soon as each app is handled)
keeps two SHAs: `sha`, the last head the tool acted on whatever the outcome, and `deployed_sha`, the last successful
deploy. The second one bounds which changes (pull/merge requests) are notified: those with a commit in
`deployed_sha...sha` (compare API; see "Forges" for which commits identify a change). This covers merge, squash and
rebase merges, several changes merged while CI was running, and changes of a failed deploy that went live with the next
successful one. Without `deployed_sha`, or if the comparison fails (rewritten history), only the head counts.
Redeploying the same SHA with `--redeploy` comments on nothing (no change is new) but still sends Telegram messages.

A deploy's result is saved right after `git:sync` returns, before notifying: an error while listing changes or
notifying (e.g. a dropped connection) must not leave a finished deploy unrecorded, which would rebuild it.

Notification channels are best-effort and independent: a failure is logged and never changes the deploy status or the
other channels. Telegram is sent synchronously so failures reach the log, with a 30s timeout: answers usually take under
1s, but some calls took about 10s (seen in 2026-09 with an invalid chat id, cause not identified), which a 10s timeout
turned into a generic error. Messages link the commit (GitHub doesn't autolink a SHA inside backticks, so comments use
an explicit Markdown link), link each pull request on its whole "#number title" and show the app URL from `dokku url
<app>` in full. Telegram messages cut the build log from its start (errors are at the end) to fit the 4096-character
limit.

## Settings

Per app, in Dokku properties, set with `auto-deploy:set`. Keys that make sense everywhere (`workflow`, `notify`,
`telegram-chat`) can also be set with `--global`, and an app without its own value uses the global one. Dokku has no
empty values (`set` without a value unsets), so "no CI" and "no notification" are the explicit value `none`, which
also overrides a global value. `repository` and `branch` are app-only and required: the old environment defaults
(stg -> `develop`, prd -> `main`) don't fit per-app settings, and guessing a branch from an app name suffix would be
magic. Values are validated when set, and each app again when a run resolves it, so a typo never falls back to a
default silently; `report` shows the same problem.

`repository` is the web URL, not `owner/name`: it says which host (and so which token) to use, and later which forge.

Secrets are never command-line arguments (visible in `ps`) nor printed: `telegram-bot-token` is read from stdin whenever
stdin isn't a terminal (a pipe or a `< file` redirection; empty input is an error, so `cat wrong-file | ...` doesn't
delete a working token, and only a run from a terminal unsets it), its format is checked without echoing it (a stray
newline would otherwise end up in the request URL and in an error message), and `report` only says whether it is set.
Unlike `dokku git:auth`, which only reads a pipe, a redirection doesn't silently unset. The forge token isn't a plugin
setting at all (next section).

## One token per forge host, from `dokku git:auth`

`git:sync` of a private repository needs `dokku git:auth <host> <user>` anyway, which stores the token in the dokku
user's `.netrc` (`${DOKKU_ROOT}/.netrc`, mode 0600, checked in Dokku's `plugins/git/internal-functions`). The plugin
reads the API token from that same entry, so there is one place to set and rotate it, and it is never duplicated in
the plugin's settings. Dokku's `.netrc` holds one entry per host, so there is one token per host per server. A
fine-grained GitHub token is bound to a single owner (user or organization): private repositories from different
owners on the same server need a classic token or a bot user with access to all of them. A per-repository token was
not implemented because putting it in the `git:sync` URL would expose it in `ps`.

The token goes in a request header (`Authorization`, or `PRIVATE-TOKEN` on GitLab), never in a URL. urllib copies every
header when it follows a redirect, so the forge client drops them when a redirect leaves the original origin (host,
port or scheme): a redirect can't send the token to another host or downgrade it to plain HTTP. An `http://`
repository URL is accepted for internal forges, and then the token does travel in clear text.

## Prior art

Checked in 2026-09: `pmac/dokku-webhook-deploy`, `mitigate-dev/deployer`, `signalwire-demos/dokku-deploy-system` and
`ledokku`. None waits for the CI of the exact commit; two keep a Dokku SSH key inside an internet-facing container, one
keeps it in GitHub secrets, and two force `apps:unlock` before deploying.
