# Home Manager fork

## Keep reconciliation on both branches

`master` tracks unstable Nixpkgs. `release-26.05` is used by strata's
`rpi500plus` host through its `home-manager-rpi` input.

Changes to writable-file reconciliation must land on both branches, including
the implementation, tests, and documentation. Check the other branch before
backporting so changes are not duplicated. Preserve stable-package compatibility.

Run `tests/modules/files/reconciliation-integration.nix` with each branch's
Nixpkgs revision from `flake.lock`; it includes the Python reconciliation tests.
Do not validate the release branch only against unstable Nixpkgs.

When publishing reconciliation changes is authorized, push both branches and
report their commits. A push does not update strata's lockfile or activate a host;
those require their own requested scope.
