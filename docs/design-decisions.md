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

## Scheduling: systemd timer + oneshot service

Alternatives: cron (needs `flock -n` to avoid overlapping runs, `timeout`, and log redirection), a `while true; sleep`
daemon (state in memory, no way to trigger a run by hand) and an app inside Dokku (would need a Dokku SSH key inside a
container). With `OnUnitInactiveSec`, the interval counts from the end of the previous run: runs never overlap and a
long build only delays the next check. Timeout (`TimeoutStartSec`), logs (`journalctl -u`) and manual runs
(`systemctl start`) come for free. Cron with `flock` works the same and is documented in the README.

## Deploying the exact commit

`dokku git:sync --build <app> <url> <sha>` receives the SHA whose CI was checked, so a push that arrives during the
check waits for its own CI. With an explicit SHA, `git:sync` moves the deploy branch with `update-ref`, without
requiring a fast-forward (checked in Dokku's `plugins/git/internal-functions`, 2026-09): a manual `git push -f` to the
app doesn't break the next automatic deploy. Side effect: `git:sync` with a SHA never changes the app's
`deploy-branch`, hence the README's `dokku git:set <app> deploy-branch main`.

Which CI run counts: the runs of the configured workflow file (`path`) for that SHA, triggered by a push to that
branch; the one with the highest `id` wins, so a green re-run replaces an earlier failure. An empty `workflow` skips
this check entirely (repositories without CI).

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

Before deploying, the tool reads the app's `GIT_REV` (`dokku config:get <app> GIT_REV`), which Dokku sets on every git
deploy (`git_build` in Dokku's `plugins/git/functions`, checked 2026-09). If it already is the branch head, the commit
is only recorded as deployed. Without this, the first run (or a lost state file) rebuilt every app, production
included, even when nothing changed. `GIT_REV` was preferred over `git:report --git-sha` because the latter is
`git rev-parse HEAD` of the bare app repo, whose `HEAD` may not point to the deploy branch. Known limit: Dokku sets
`GIT_REV` before building, so after a failed manual deploy of commit X it says X; if X then becomes the branch head,
the tool records it without building. `poll --force <app>` rebuilds regardless.

## Failed deploys are not retried automatically

A SHA whose deploy failed is recorded and left alone, so a broken commit isn't rebuilt every minute. Retrying is
explicit (`poll --force <app>`), which also puts an app back on its branch head after a manual deploy.

## State and notified pull requests

The state file (JSON, written to a temp file and renamed, saved after each target) keeps two SHAs per app: `sha`, the
last head the tool acted on whatever the outcome, and `deployed_sha`, the last successful deploy. The second one bounds
which PRs are notified: PRs whose `merge_commit_sha` is in `deployed_sha...sha` (compare API). This covers merge,
squash and rebase merges, several PRs merged while CI was running, and PRs of a failed deploy that went live with the
next successful one. Without `deployed_sha`, or if the comparison fails (rewritten history), only the head counts.
Redeploying the same SHA with `--force` comments on no PR (none is new) but still sends Telegram messages.

Notification channels are best-effort and independent: a failure is logged and never changes the deploy status or the
other channels. Telegram is sent synchronously so failures reach the log, with a 30s timeout: answers usually take under
1s, but some calls took about 10s (seen in 2026-09 with an invalid chat id, cause not identified), which a 10s timeout
turned into a generic error. Messages link the commit (GitHub doesn't autolink a SHA inside backticks, so comments use
an explicit Markdown link), link each pull request on its whole "#number title" and show the app URL from `dokku url
<app>` in full. Telegram messages cut the build log from its start (errors are at the end) to fit the 4096-character
limit.

## Configuration

TOML (`tomllib`, stdlib since Python 3.11). Unknown sections and keys are errors, so a typo never falls back to a
default silently. `workflow` and `telegram-chat` can be set in `[defaults]` and are inherited only when a `[[repo]]`
doesn't set them: a value set in the repo wins even if empty, which is what makes `workflow = ""` mean "no CI". For
the other keys an empty string means the built-in default. Environments are tables (`stg = {}`, `prd = {}`): the
table's presence enables the environment. App names get an explicit suffix (`-stg`, `-prd`); production is never the
bare name.

Secrets live in `0600` files; the config only holds their paths, and nothing secret is ever passed as a command-line
argument (visible in `ps`).

## One GitHub token per server

The same token is used for the API and, through `dokku git:auth github.com`, for `git:sync` of private repositories.
Dokku's `.netrc` holds one entry per host, so there is one token for `github.com` per server. A fine-grained token is
bound to a single owner (user or organization): private repositories from different owners on the same server need a
classic token or a bot user with access to all of them. A per-repository token was not implemented because putting it
in the `git:sync` URL would expose it in `ps`.

## Prior art

Checked in 2026-09: `pmac/dokku-webhook-deploy`, `mitigate-dev/deployer`, `signalwire-demos/dokku-deploy-system` and
`ledokku`. None waits for the CI of the exact commit; two keep a Dokku SSH key inside an internet-facing container, one
keeps it in GitHub secrets, and two force `apps:unlock` before deploying.
