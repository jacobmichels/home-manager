# Daily upstream sync

This fork checks `nix-community/home-manager:master` daily at 09:17 UTC and
opens one PR from `automation/upstream-sync` into `master`. An existing open
sync PR is left untouched until reviewed and merged. Use a **merge commit**
when merging it: squash/rebase loses the ancestry needed for the next sync.
The workflow never merges PRs automatically.

GitHub API calls use the built-in `GITHUB_TOKEN`. Branch pushes use a write deploy
key scoped to this repository, stored as `UPSTREAM_SYNC_DEPLOY_KEY`, because
upstream merges can modify workflow files. The key is available only to the
publication job. For AI conflict resolution,
add `OPENAI_API_KEY` under **Settings → Secrets and variables → Actions**. This
uses separately billed OpenAI API access, not ChatGPT subscription usage. Optionally
set the repository variable `CODEX_SYNC_MODEL`; otherwise the Codex action uses
its default. Codex only runs when Git reports conflicts, with a 15-minute timeout.
The timeout is not a monetary spending cap; use API project budget controls too.

## Decisions and retries

The fork's intentional customization is `programs.fish.suppressGreeting`
(default `false`), introduced by `0fe59907841143c3ce11c992085def783f1be660`,
including its regression test. The conflict-resolution prompt preserves it.

Ambiguous conflicts, a missing API key, and failed Codex runs create/update the
issue **Upstream sync needs a decision**, assigned to Jacob. Reply on that issue:

```
/upstream-sync Preserve both upstream's new Fish tests and our suppressGreeting test.
```

Only `jacobmichels` can trigger a retry this way. Each retry starts a fresh merge;
include the complete decision in the command. Repeated daily runs do not spend
API usage retrying the same blocked base/upstream pair. After adding an API key,
use **Actions → Daily upstream sync → Run workflow** to retry immediately.
The manual trigger also accepts guidance and a `dry_run` option. A dry run may
use the API when conflicts exist, but never pushes or posts to GitHub.

Successful merge candidates run formatting, Fish regression tests and the
module-maintainer docs build on Linux and macOS. These are focused checks,
not the entire Home Manager suite. Failed checks produce a **draft PR** with the
run linked for inspection. GITHUB_TOKEN-created PRs do not trigger the normal
pull_request workflows, so these checks run explicitly in this workflow.

The runner that resolves conflicts has a read-only GitHub token and does not
retain checkout credentials. Publication runs in a fresh job with write access;
it imports the merge bundle without executing candidate code. It verifies the
merge parents and refuses to overwrite unmerged branch work or publish against
a changed master. If you close a sync PR without merging it, delete its branch
when ready to discard that proposal and allow another attempt.

Merge the setup PR to activate the daily schedule and comment/manual triggers.
Pushes to `automation/daily-upstream-sync` run orchestration tests and a real
merge preparation in dry-run mode, without API calls or GitHub publication.
GitHub may disable schedules in inactive public repositories after 60 days;
check the Actions page if runs stop.
