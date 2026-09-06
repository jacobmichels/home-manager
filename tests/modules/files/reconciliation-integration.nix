# Run: nix-build tests/modules/files/reconciliation-integration.nix --no-out-link
{
  pkgs ? import <nixpkgs> { },
}:
let
  make =
    text: mutable:
    (import ../../../modules {
      inherit pkgs;
      configuration = {
        home.username = "hm-test";
        home.homeDirectory = "/homeless-shelter";
        home.stateVersion = "25.05";
        manual.manpages.enable = false;
        home.file.config = {
          enable = text != null;
          text = if text == null then "" else text;
          reconciliation.enable = mutable;
          onChange = ''touch "$HOME/hook-ran"'';
        };
        home.file.static.text = if text == null then "removed" else text;
      };
    }).config;
  first = make "theme=light\nfont=14\n" true;
  second = make "theme=light\nfont=16\n" true;
  conflict = make "theme=catppuccin\nfont=16\n" true;
  immutable = make "theme=light\nfont=14\n" false;
  removed = make null false;
  generation =
    cfg:
    pkgs.runCommand "reconciliation-test-generation" { } ''
      mkdir "$out"
      ln -s ${cfg.home-files} "$out/home-files"
      ${cfg.home.extraBuilderCommands}
    '';
  activate =
    cfg:
    pkgs.writeShellScript "activate-files" ''
      set -euo pipefail
      export HOME_MANAGER_BACKUP_EXT="" HOME_MANAGER_BACKUP_OVERWRITE=""
      export HOME_MANAGER_BACKUP_COMMAND=""
      export VERBOSE_ARG=""
      oldGenPath="$1"
      newGenPath=${generation cfg}
      ${cfg.lib.bash.initHomeManagerLib}
      ${cfg.home.activation.checkReconciledFiles.data}
      ${cfg.home.activation.checkLinkTargets.data}
      ${cfg.home.activation.checkFilesChanged.data}
      run touch "$HOME/write-boundary"
      ${cfg.home.activation.linkGeneration.data}
      ${cfg.home.activation.onFilesChange.data}
    '';
in
pkgs.runCommand "reconciliation-integration"
  {
    nativeBuildInputs = [
      pkgs.bash
      pkgs.coreutils
      pkgs.findutils
      pkgs.diffutils
      pkgs.gettext
      pkgs.gnused
      pkgs.gnugrep
      pkgs.python3
    ];
  }
  ''
    export PYTHONDONTWRITEBYTECODE=1
    export RECONCILE_MODULE=${../../../modules/files/reconcile.py}
    python3 ${./reconcile_test.py}
    export HOME="$TMPDIR/home"
    mkdir "$HOME"
    DRY_RUN=1 ${activate first} ""
    test ! -e "$HOME/config"
    test ! -e "$HOME/static"
    test ! -e "$HOME/write-boundary"
    ${activate first} ""
    test -f "$HOME/config" && test ! -L "$HOME/config" && test -w "$HOME/config"
    test -L "$HOME/static"
    test -e "$HOME/hook-ran"
    rm "$HOME/hook-ran"
    ${activate second} ${generation first}
    cmp "$HOME/config" ${second.home-files}/config
    test -e "$HOME/hook-ran"
    rm "$HOME/hook-ran"
    sed -i 's/theme=light/theme=dark/' "$HOME/config"
    ${activate second} ${generation second} 2> "$TMPDIR/notices"
    grep -q 'LOCAL CHANGES:.*config' "$TMPDIR/notices"
    # Report persistent local edits again on the next activation.
    ${activate second} ${generation second} 2> "$TMPDIR/notices"
    grep -q 'LOCAL CHANGES:.*config' "$TMPDIR/notices"
    test ! -e "$HOME/hook-ran"
    grep -q 'theme=dark' "$HOME/config"
    # Roll back the independent font edit, preserving the application's theme.
    ${activate first} ${generation second} 2> "$TMPDIR/notices"
    grep -q 'MERGE READY:.*config' "$TMPDIR/notices"
    grep -q 'theme=dark' "$HOME/config"
    grep -q 'font=14' "$HOME/config"
    cp "$HOME/config" "$TMPDIR/before"
    cp -P "$HOME/static" "$TMPDIR/static-before"
    rm "$HOME/write-boundary"
    if ${activate conflict} ${generation first} > "$TMPDIR/log" 2>&1; then exit 1; fi
    grep -q 'DIVERGENCE:.*config' "$TMPDIR/log"
    grep -q 'Previous generated file:' "$TMPDIR/log"
    grep -q 'New desired file:' "$TMPDIR/log"
    grep -q 'Keep local edits:' "$TMPDIR/log"
    grep -q 'Accept Nix:' "$TMPDIR/log"
    grep -q 'Failed activation does not advance' "$TMPDIR/log"
    cmp "$HOME/config" "$TMPDIR/before"
    test "$(readlink "$HOME/static")" = "$(readlink "$TMPDIR/static-before")"
    test ! -e "$HOME/write-boundary"
    # Use the exact generation-local command printed in divergence guidance.
    mkdir -p "$HOME/.local/state/home-manager/gcroots"
    ln -s ${generation first} "$HOME/.local/state/home-manager/gcroots/current-home"
    ${generation conflict}/reconcile --merge-declared "$HOME/config"
    cmp "$HOME/config" "$TMPDIR/before"
    ${activate conflict} ${generation first}
    cmp "$HOME/config" ${conflict.home-files}/config
    ln -sfn ${generation conflict} "$HOME/.local/state/home-manager/gcroots/current-home"
    echo 'local=kept' >> "$HOME/config"
    ${generation first}/reconcile --replace config
    grep -q 'local=kept' "$HOME/config"
    ${activate first} ${generation conflict}
    cmp "$HOME/config" ${first.home-files}/config
    # First activation must fail for a foreign file before any other file is linked.
    export HOME="$TMPDIR/foreign"
    mkdir "$HOME"
    echo foreign > "$HOME/config"
    if ${activate first} "" > "$TMPDIR/log" 2>&1; then exit 1; fi
    grep -q 'No previous generated file is available' "$TMPDIR/log"
    grep -q foreign "$HOME/config"
    test ! -e "$HOME/write-boundary"
    test ! -e "$HOME/static"
    # Existing symlink behavior, followed by migration to a writable file.
    export HOME="$TMPDIR/immutable"
    mkdir "$HOME"
    ${activate immutable} ""
    test -L "$HOME/config"
    ${activate first} ${generation immutable}
    test ! -L "$HOME/config"
    cmp "$HOME/config" ${first.home-files}/config
    # A declaration removal relinquishes ownership, even if the application
    # replaced the writable file with an old Home Manager store symlink.
    rm "$HOME/config"
    ln -s ${immutable.home-files}/config "$HOME/config"
    ${activate removed} ${generation first}
    test -L "$HOME/config"
    touch "$out"
  ''
