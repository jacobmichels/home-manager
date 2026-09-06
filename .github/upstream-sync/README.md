# Upstream sync

The private job on `rpi500plus` checks upstream daily at 09:17 UTC and checks
pending validation and owner replies every 15 minutes. Its service, scripts,
prompt, and tests are declared in Jacob's Strata configuration:

- `modules/features/home-manager-upstream-sync.nix`
- `modules/features/_home-manager-upstream-sync/`

Codex runs on the Pi using a dedicated ChatGPT subscription login. No OpenAI API
key or ChatGPT login is stored in this repository's Actions secrets. GitHub Actions
only performs Linux/macOS validation on pushes to `automation/upstream-sync`.
This also validates rebased candidates that cannot be merged into the old history.

Routine updates auto-merge after both platform jobs succeed. A PR stays open if
Git needed conflict resolution, Fish/automation/release files changed, modules
were removed, commit subjects flag compatibility changes, or the update exceeds
100 commits or 100 changed files. Failed validation produces a draft PR. These
are explicit attention rules, not a guarantee that all behavioral changes are detected.
The Pi rebases fork commits onto upstream without creating merge commits, then
publishes the exact tested candidate with an explicit force-with-lease on master.
A concurrent change to master rejects the update. Fork commit IDs change after
each rebase; existing checkouts must account for rewritten history. For reviewed
updates, publish the exact tested candidate with the same explicit lease rather
than using GitHub’s merge-commit button. Upstream’s own history is preserved.

For blocked conflicts, reply on **Upstream sync needs a decision** with
`/upstream-sync <complete decision>`. Only Jacob's commands are accepted. The Pi
will pick up the command within 15 minutes. Existing open sync PRs are preserved.

On the Pi:

- `home-manager-upstream-sync-login`: sign in with ChatGPT for this job.
- `home-manager-upstream-sync --sync`: check upstream immediately.
- `home-manager-upstream-sync`: check pending work and decision replies.
- `systemctl --user list-timers 'home-manager-upstream-*'`: inspect schedules.
- `journalctl --user -u home-manager-upstream-sync -u home-manager-upstream-follow-up`:
  inspect service results.

Private state and refreshed credentials remain under
`~/.local/state/home-manager-upstream-sync/` on the Pi. Do not copy them into GitHub.
