"""Run with python3 -m unittest discover -s tests/modules/files -p '*_test.py'."""
import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "reconcile", os.environ.get("RECONCILE_MODULE") or
    Path(__file__).resolve().parents[3] / "modules/files/reconcile.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class Reconciliation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.live = self.home / "config"

    def generation(self, name, text, mutable=True):
        gen = self.root / name
        (gen / "home-files").mkdir(parents=True)
        if text is not None:
            (gen / "home-files/config").write_bytes(text)
        (gen / "reconciliation.json").write_text(json.dumps(["config"] if mutable else []))
        return str(gen)

    def activate(self, old, new):
        r.apply(self.home, r.check(self.home, old, new))

    def notice_output(self, operation):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            operation()
        return output.getvalue()

    def test_local_notice_once_per_contents_and_declaration(self):
        old = self.generation("old", b"size=20\n")
        self.live.write_bytes(b"size=24\n")
        before = r.snapshot(self.live)
        first = self.notice_output(lambda: r.report_status(self.home, old, old))
        self.assertIn("LOCAL CHANGES:", first)
        self.assertIn(f"Show files: {old}/reconcile --check --verbose", first)
        self.assertIn("informational, not conflicts", first)
        again = self.notice_output(lambda: r.report_status(self.home, old, old))
        self.assertNotIn("LOCAL CHANGES:", again)
        self.assertIn("1 file(s) have previously reported", again)
        self.assertIn(f"Show files: {old}/reconcile --check --verbose", again)
        self.assertEqual(r.snapshot(self.live), before)
        verbose = self.notice_output(lambda: r.report_status(self.home, old, old, verbose=True))
        self.assertIn("LOCAL CHANGES:", verbose)
        self.assertIn(f"{old}/reconcile --replace config", verbose)
        self.assertIn(f"{old}/reconcile --export config", verbose)
        self.assertIn("Approval alone does not change the file", verbose)
        self.assertIn("update the Nix declaration", verbose)
        self.assertEqual(r.snapshot(self.live), before)
        self.assertFalse(r.approval_path(self.home, "config").exists())
        self.live.write_bytes(b"size=25\n")
        changed = self.notice_output(lambda: r.report_status(self.home, old, old))
        self.assertIn("LOCAL CHANGES:", changed)
        new = self.generation("new", b"size=22\n")
        changed_base = self.notice_output(lambda: r.report_status(self.home, new, new))
        self.assertIn("LOCAL CHANGES:", changed_base)

    def test_preflight_does_not_acknowledge_notices(self):
        old = self.generation("old", b"size=20\n")
        self.live.write_bytes(b"size=24\n")
        plan = r.check(self.home, old, old)
        self.assertFalse((self.home / ".local/state").exists())
        output = self.notice_output(lambda: r.apply(self.home, plan))
        self.assertIn("LOCAL CHANGES:", output)
        output = self.notice_output(lambda: r.apply(self.home, r.check(self.home, old, old)))
        self.assertNotIn("LOCAL CHANGES:", output)

    def test_notification_history_does_not_suppress_conflicts(self):
        old = self.generation("old", b"size=20\n")
        new = self.generation("new", b"size=30\n")
        self.live.write_bytes(b"size=24\n")
        self.notice_output(lambda: r.report_status(self.home, old, old))
        before = r.snapshot(self.live)
        with self.assertRaises(r.Divergence):
            r.report_status(self.home, old, new)
        self.assertEqual(r.snapshot(self.live), before)

    def test_broken_notification_cache_is_nonfatal(self):
        old = self.generation("old", b"size=20\n")
        self.live.write_bytes(b"size=24\n")
        cache = self.home / ".local/state/home-manager/reconciliation/notices"
        cache.parent.mkdir(parents=True)
        cache.symlink_to(self.root)
        output = self.notice_output(lambda: r.report_status(self.home, old, old))
        self.assertIn("LOCAL CHANGES:", output)
        self.assertIn("Could not remember", output)
        self.assertFalse((self.root / (r.digest(b"config") + ".json")).exists())

    def test_create(self):
        self.activate("", self.generation("new", b"hello\n"))
        self.assertFalse(self.live.is_symlink())
        self.assertEqual(self.live.read_bytes(), b"hello\n")
        self.assertEqual(self.live.stat().st_mode & 0o777, 0o600)

    def test_three_way_states(self):
        a = b"theme=light\nfont-size=14\n"
        for b, c, expected in [
            (a, b"new\n", b"new\n"),
            (b"app\n", a, b"app\n"),
            (b"same\n", b"same\n", b"same\n"),
            (b"theme=dark\nfont-size=14\n", b"theme=light\nfont-size=16\n",
             b"theme=dark\nfont-size=16\n"),
        ]:
            with self.subTest(b=b, c=c):
                self.assertEqual(r.merge(a, b, c), expected)

    def test_conflict_does_not_write_any_file(self):
        old = self.generation("old", b"theme=light\n")
        new = self.generation("new", b"theme=catppuccin\n")
        self.live.write_bytes(b"theme=dark\n")
        (Path(new) / "home-files/aaa").write_bytes(b"create me")
        (Path(new) / "reconciliation.json").write_text('["aaa", "config"]')
        before = r.snapshot(self.live)
        with self.assertRaisesRegex(r.Divergence, "DIVERGENCE:.*config"):
            self.activate(old, new)
        self.assertEqual(r.snapshot(self.live), before)
        self.assertFalse((self.home / "aaa").exists())

    def test_foreign_file_even_identical(self):
        self.live.write_bytes(b"same")
        with self.assertRaises(r.Divergence):
            self.activate("", self.generation("new", b"same"))
        self.assertEqual(self.live.read_bytes(), b"same")

    def test_symlink_migration(self):
        old = self.generation("old", b"old", False)
        self.live.symlink_to(Path(old) / "home-files/config")
        self.activate(old, self.generation("new", b"new"))
        self.assertFalse(self.live.is_symlink())
        self.assertEqual(self.live.read_bytes(), b"new")

    def test_removal_preserves_live(self):
        old = self.generation("old", b"old")
        self.live.write_bytes(b"app")
        self.activate(old, self.generation("new", None, False))
        self.assertEqual(self.live.read_bytes(), b"app")

    def test_return_to_symlinks_rejected_even_if_identical(self):
        old = self.generation("old", b"same")
        self.live.write_bytes(b"same")
        with self.assertRaises(r.Divergence):
            self.activate(old, self.generation("new", b"same", False))

    def test_deleted_or_replaced_file(self):
        old = self.generation("old", b"old")
        new = self.generation("new", b"new")
        with self.assertRaises(r.Divergence):
            self.activate(old, new)
        self.live.symlink_to(self.root / "missing")
        with self.assertRaises(r.Divergence):
            self.activate(old, new)
        self.assertTrue(self.live.is_symlink())

    def test_mode_preserved_and_changes_rejected(self):
        old = self.generation("old", b"old")
        new = self.generation("new", b"new")
        self.live.write_bytes(b"old")
        self.live.chmod(0o640)
        self.activate(old, new)
        self.assertEqual(self.live.stat().st_mode & 0o777, 0o640)
        (Path(old) / "home-files/config").chmod(0o755)
        with self.assertRaises(r.Divergence):
            self.activate(new, old)

    def test_rollback(self):
        old = self.generation("old", b"theme=light\nfont=14\n")
        new = self.generation("new", b"theme=light\nfont=16\n")
        self.live.write_bytes(b"theme=dark\nfont=16\n")
        self.activate(new, old)
        self.assertEqual(self.live.read_bytes(), b"theme=dark\nfont=14\n")

    def test_changed_after_check(self):
        new = self.generation("new", b"new")
        plan = r.check(self.home, "", new)
        self.live.write_bytes(b"app")
        with self.assertRaises(r.Divergence):
            r.apply(self.home, plan)
        self.assertEqual(self.live.read_bytes(), b"app")

    def test_symlink_parent(self):
        new = self.generation("new", b"new")
        plan = r.check(self.home, "", new)
        plan[0]["name"] = "parent/config"
        (self.home / "parent").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(r.Divergence):
            r.apply(self.home, plan)

    def test_binary_and_ambiguous_insertions(self):
        with self.assertRaises(r.Divergence):
            r.merge(b"", b"", b"\0")
        with self.assertRaises(r.Divergence):
            r.merge(b"one\n", b"insert\none\n", b"other\none\n")

    def test_default_files_are_not_reconciled(self):
        new = self.generation("new", b"new", False)
        self.live.write_bytes(b"foreign")
        self.assertEqual(r.check(self.home, "", new), [])

    def test_special_metadata_rejected(self):
        old = self.generation("old", b"old")
        new = self.generation("new", b"new")
        self.live.write_bytes(b"old")
        self.live.chmod(0o1600)
        with self.assertRaises(r.Divergence):
            self.activate(old, new)
        self.assertEqual(self.live.read_bytes(), b"old")

    def test_clean_insertions_deletions_and_identical_edits(self):
        self.assertEqual(r.merge(b"a\nb\nc\nd\n", b"a\nc\nd\n",
                                 b"a\nb\nc\nnew\nd\n"), b"a\nc\nnew\nd\n")
        self.assertEqual(r.merge(b"a\nb\nc\n", b"A\nb\nc\n",
                                 b"A\nb\nC\n"), b"A\nb\nC\n")

    def test_shared_replacement_with_surrounding_live_additions(self):
        old = self.generation("old", b"theme=light\nfont-size=14\n")
        new = self.generation("new", b"theme=light\nfont-size=20\n")
        live = b"foo=bar\ntheme=light\nhello=world\nfont-size=20\nmeow=mix\n"
        self.live.write_bytes(live)
        before = r.snapshot(self.live)
        self.activate(old, new)
        self.assertEqual(r.snapshot(self.live), before)

    def test_shared_replacement_with_declarative_additions(self):
        self.assertEqual(r.merge(b"size=14\n", b"size=20\n",
                                 b"before\nsize=20\nafter\n"),
                         b"before\nsize=20\nafter\n")

    def test_surrounding_additions_do_not_hide_conflicts(self):
        for live in (b"before\nsize=12\nafter\n",
                     b"size=20\nsize=20\n",
                     b"size=14\nsize=20\n"):
            with self.subTest(live=live), self.assertRaises(r.Divergence):
                r.merge(b"size=14\n", live, b"size=20\n")

    def test_shared_replacement_does_not_hide_another_conflict(self):
        with self.assertRaises(r.Divergence):
            r.merge(b"size=14\nseparator\ntheme=light\n",
                    b"before\nsize=20\nafter\nseparator\ntheme=dark\n",
                    b"size=20\nseparator\ntheme=catppuccin\n")

    def test_resolution_is_bound_to_exact_inputs(self):
        old = self.generation("old", b"size=14\n")
        new = self.generation("new", b"size=20\n")
        other = self.generation("other", b"size=22\n")
        self.live.write_bytes(b"size=12\n")
        r.resolve(self.home, old, new, str(self.live), replace=True)
        with self.assertRaises(r.Divergence):
            self.activate(old, other)
        self.live.write_bytes(b"size=13\n")
        with self.assertRaises(r.Divergence):
            self.activate(old, new)
        self.assertEqual(self.live.read_bytes(), b"size=13\n")

    def test_resolution_rejects_foreign_and_symlink_files(self):
        new = self.generation("new", b"size=20\n")
        self.live.write_bytes(b"size=12\n")
        with self.assertRaises(r.Divergence):
            r.resolve(self.home, "", new, "config", replace=True)
        old = self.generation("old", b"size=14\n")
        self.live.unlink()
        self.live.symlink_to(Path(old) / "home-files/config")
        with self.assertRaises(r.Divergence):
            r.resolve(self.home, old, new, "config", replace=True)

    def test_live_additions_beside_unchanged_line_update_normally(self):
        base = b"theme=light\nfont-size=20\n"
        desired = b"theme=light\nfont-size=30\n"
        old = self.generation("old", base)
        new = self.generation("new", desired)
        for live in (
            b"theme=light\nmeow=mix\nfont-size=20\n",
            b"theme=light\nfont-size=20\nhello=world\n",
            b"foo=bar\ntheme=light\nmeow=mix\nfont-size=20\nhello=world\n",
        ):
            with self.subTest(live=live):
                self.live.write_bytes(live)
                self.activate(old, new)
                self.assertEqual(self.live.read_bytes(),
                                 live.replace(b"font-size=20", b"font-size=30"))
                self.assertFalse(r.approval_path(self.home, "config").exists())

    def test_shell_additions_stay_on_each_side_of_updated_command(self):
        base = b'#!/bin/sh\necho old\n'
        live = b'#!/bin/sh\n# before\necho old\naudit\n'
        desired = b'#!/bin/sh\necho new\n'
        self.assertEqual(r.merge(base, live, desired),
                         b'#!/bin/sh\n# before\necho new\naudit\n')

    def test_boundary_insertion_exception_does_not_hide_ambiguity(self):
        for base, live, desired in (
            (b"a\nb\n", b"a\nextra\nb\n", b"a\n"),  # deletion
            (b"a\nb\n", b"extra\na\nb\n", b"A\nB\n"),  # block replacement
            (b"a\n", b"extra\na\n", b"other\na\n"),  # competing insertions
            (b"a\na\n", b"extra\na\na\n", b"A\na\n"),  # repeated anchor
            (b"a\nb\n", b"extra\na\nb\na\n", b"A\nb\n"),  # duplicated live anchor
        ):
            with self.subTest(base=base, live=live, desired=desired):
                with self.assertRaises(r.Divergence):
                    r.merge(base, live, desired)

    def test_shell_independent_function_edits(self):
        base = (b'#!/bin/sh\nprepare() {\n  echo "preparing"\n}\n\n'
                b'finish() {\n  echo "done"\n}\nprepare\nfinish\n')
        live = base.replace(b'echo "preparing"', b'# local instrumentation\n  echo "starting"')
        desired = base.replace(b'echo "done"', b'printf "%s\\n" "finished"')
        old = self.generation("old", base)
        new = self.generation("new", desired)
        self.live.write_bytes(live)
        self.activate(old, new)
        self.assertEqual(self.live.read_bytes(),
                         live.replace(b'echo "done"', b'printf "%s\\n" "finished"'))

    def test_shell_declared_resolution_preserves_comments_commands_and_mode(self):
        base = b'#!/bin/sh\nMODE="default"\nexec worker "$MODE"\n'
        live = (b'#!/bin/sh\n# Keep my diagnostics\nset -x\nMODE="local"\n'
                b'printf "%s\\n" "$MODE" >&2\nexec worker "$MODE"\n')
        desired = base.replace(b'"default"', b'"production"')
        old = self.generation("old", base)
        new = self.generation("new", desired)
        for generation in (old, new):
            (Path(generation) / "home-files/config").chmod(0o755)
        self.live.write_bytes(live)
        self.live.chmod(0o700)
        before = r.snapshot(self.live)
        with self.assertRaises(r.Divergence):
            self.activate(old, new)
        self.assertEqual(r.snapshot(self.live), before)
        workspace = r.export_conflict(self.home, old, new, "config")
        (workspace / "candidate").write_bytes(live.replace(b'"local"', b'"production"'))
        r.accept_conflict(self.home, old, new, workspace, lambda _: "yes")
        self.assertEqual(r.snapshot(self.live), before)
        receipt = json.loads(r.approval_path(self.home, "config").read_text())
        self.assertEqual(Path(receipt["backup"]).read_bytes(), live)
        self.activate(old, new)
        self.assertEqual(self.live.read_bytes(), live.replace(b'"local"', b'"production"'))
        self.assertEqual(self.live.stat().st_mode & 0o777, 0o700)

    def test_shell_quoted_heredoc_preserves_literal_content(self):
        base = (b"#!/bin/sh\ncat <<'EOF'\n$HOME is literal\n"
                b"$(do_not_execute)\nEOF\n\necho done\n")
        live = base.replace(b'$HOME is literal', b'$HOME and `commands` stay literal')
        desired = base.replace(b'echo done', b'echo finished')
        self.assertEqual(r.merge(base, live, desired),
                         live.replace(b'echo done', b'echo finished'))

    def test_shell_continuation_and_no_final_newline(self):
        base = (b'#!/bin/sh\nworker \\\n  --mode default \\\n  --verbose\n\n'
                b'printf "%s" "done"')
        live = base.replace(b'--mode default', b'--mode "local value"')
        desired = base.replace(b'"done"', b'"finished"')
        self.assertEqual(r.merge(base, live, desired),
                         live.replace(b'"done"', b'"finished"'))

    def test_shell_ambiguous_resolution_leaves_everything_untouched(self):
        # These are text fixtures, never executed. Both ordinary activation and
        # the removed declared-preferred resolver must refuse without writing.
        cases = {
            "shared value prefix does not identify local assignment": (
                b'MODE="default"\n', b'# local comment\nMODE="local"\naudit\n',
                b'MODE="declared"\n'),
            "repeated assignments": (
                b'MODE=default\n', b'MODE=local\nMODE=override\n', b'MODE=declared\n'),
            "competing inserted commands": (
                b'echo ready\n', b'local_hook\necho ready\n',
                b'declared_hook\necho ready\n'),
            "rewritten conditional with local command": (
                b'if test -f old; then\n  old_command\nfi\n',
                b'if test -f local; then\n  local_command\n  audit\nfi\n',
                b'if test -f declared; then\n  declared_command\nfi\n'),
            "heredoc rewritten with extra local content": (
                b"cat <<'EOF'\nred\nyellow\nEOF\n",
                b"cat <<'EOF'\ngreen\nkeep this\norange\nEOF\n",
                b"cat <<'EOF'\nblue\npurple\nEOF\n"),
            "continued command rewritten with local argument": (
                b'worker \\\n  --first old \\\n  --second old\n',
                b'worker \\\n  --first local \\\n  --keep-me \\\n  --second local\n',
                b'worker \\\n  --first declared \\\n  --second declared\n'),
        }
        for index, (name, (base, live, desired)) in enumerate(cases.items()):
            with self.subTest(name=name):
                old = self.generation(f"old-{index}", b'#!/bin/sh\n' + base)
                new = self.generation(f"new-{index}", b'#!/bin/sh\n' + desired)
                self.live.write_bytes(b'#!/bin/sh\n' + live)
                before = r.snapshot(self.live)
                with self.assertRaises(r.Divergence):
                    self.activate(old, new)
                self.assertEqual(r.snapshot(self.live), before)
                with self.assertRaises(r.Divergence):
                    r.resolve(self.home, old, new, "config", replace=False)
                self.assertEqual(r.snapshot(self.live), before)
                self.assertFalse(r.approval_path(self.home, "config").exists())
                self.assertEqual(list(self.home.glob("config.hm-backup-*")), [])

    def candidate_workspace(self):
        old = self.generation("old", b"size=20\n")
        new = self.generation("new", b"size=30\n")
        self.live.write_bytes(b"size=24\n# keep me\n")
        workspace = r.export_conflict(self.home, old, new, "config")
        return old, new, workspace

    def test_export_and_accept_reviewed_candidate(self):
        old, new, workspace = self.candidate_workspace()
        before = r.snapshot(self.live)
        self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
        for filename, expected in (("base", b"size=20\n"), ("declared", b"size=30\n"),
                                   ("live", self.live.read_bytes()), ("candidate", self.live.read_bytes())):
            self.assertEqual((workspace / filename).read_bytes(), expected)
            self.assertEqual((workspace / filename).stat().st_mode & 0o777, 0o600)
        result = b"size=30\n# keep me\n# reviewed\n"
        (workspace / "candidate").write_bytes(result)
        self.assertTrue(r.accept_conflict(self.home, old, new, workspace, lambda _: "yes"))
        self.assertEqual(r.snapshot(self.live), before)
        receipt = json.loads(r.approval_path(self.home, "config").read_text())
        self.assertEqual(Path(receipt["backup"]).read_bytes(), b"size=24\n# keep me\n")
        self.activate(old, new)
        self.assertEqual(self.live.read_bytes(), result)
        self.assertFalse(r.approval_path(self.home, "config").exists())
        with self.assertRaises(r.Divergence):
            r.accept_conflict(self.home, old, new, workspace, lambda _: "yes")

    def test_cleanup_retention_pending_and_dry_run(self):
        old, new, _ = self.candidate_workspace()
        backups = []
        for i in range(6):
            r.resolve(self.home, old, new, "config")
            receipt = r.read_record(r.approval_path(self.home, "config"))
            backups.append(Path(receipt["backup"]))
            records = self.home / r.STATE / "backups"
            record_path = records / (r.digest(os.fsencode(os.path.relpath(backups[-1], self.home))) + ".json")
            record = r.read_record(record_path)
            record["created"] = i
            r.write_record(record_path, record)
            if i == 0:
                pending = receipt
        # Even an old or stale pending approval protects its backup.
        r.write_record(r.approval_path(self.home, "config"), pending)
        orphan = self.home / "config.hm-backup-legacy"
        orphan.write_text("untracked")
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        output = self.notice_output(lambda: r.cleanup(self.home, dry_run=True, now=40 * 86400))
        self.assertIn("Would remove backup:", output)
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        r.cleanup(self.home, now=40 * 86400)
        self.assertEqual([p.exists() for p in backups], [True, False, False, True, True, True])
        self.assertTrue(orphan.exists())

    def test_cleanup_keeps_recent_and_modified_backups(self):
        old, new, _ = self.candidate_workspace()
        backups = []
        for _ in range(5):
            r.resolve(self.home, old, new, "config")
            backups.append(Path(r.read_record(r.approval_path(self.home, "config"))["backup"]))
        r.cleanup(self.home)
        self.assertTrue(all(p.exists() for p in backups))
        backups[0].write_text("edited backup")
        r.cleanup(self.home, now=r.time.time() + 40 * 86400)
        self.assertTrue(backups[0].exists())
        self.assertFalse(backups[1].exists())

    def test_workspace_cleanup_only_after_success(self):
        old, new, workspace = self.candidate_workspace()
        unresolved = r.export_conflict(self.home, old, new, "config")
        r.accept_conflict(self.home, old, new, workspace, lambda _: "yes")
        r.cleanup(self.home)
        self.assertTrue(workspace.exists())
        plan = r.check(self.home, old, new)
        r.apply(self.home, plan)
        # A later failing activation hook never calls finish_activation.
        r.cleanup(self.home)
        self.assertTrue(workspace.exists())
        r.finish_activation(self.home, plan)
        self.assertFalse(workspace.exists())
        self.assertFalse(workspace.with_suffix(".json").exists())
        self.assertTrue(unresolved.exists())

    def test_workspace_edits_after_approval_are_preserved(self):
        old, new, workspace = self.candidate_workspace()
        r.accept_conflict(self.home, old, new, workspace, lambda _: "yes")
        plan = r.check(self.home, old, new)
        r.apply(self.home, plan)
        (workspace / "candidate").write_text("more unfinished work")
        r.finish_activation(self.home, plan)
        self.assertTrue(workspace.exists())

    def test_cleanup_rejects_backup_symlink(self):
        old, new, _ = self.candidate_workspace()
        for _ in range(4):
            r.resolve(self.home, old, new, "config")
        records = sorted((self.home / r.STATE / "backups").glob("*.json"),
                         key=lambda p: r.read_record(p)["created"])
        backup = self.home / r.read_record(records[0])["backup"]
        backup.unlink()
        backup.symlink_to(self.live)
        r.cleanup(self.home, now=r.time.time() + 40 * 86400)
        self.assertTrue(backup.is_symlink())
        self.assertTrue(self.live.exists())

    def test_resolved_workspace_dry_run_and_unknown_files(self):
        _, _, workspace = self.candidate_workspace()
        metadata_path = workspace.with_suffix(".json")
        metadata = r.read_record(metadata_path)
        metadata["resolved"] = r.workspace_snapshot(workspace)
        r.write_record(metadata_path, metadata)
        output = self.notice_output(lambda: r.cleanup(self.home, dry_run=True))
        self.assertIn("Would remove resolved workspace:", output)
        self.assertTrue(workspace.exists())
        extra = workspace / "notes"
        extra.write_text("unfinished notes")
        r.cleanup(self.home)
        self.assertTrue(extra.exists())
        extra.unlink()
        r.cleanup(self.home)
        self.assertFalse(workspace.exists())
        self.assertFalse(metadata_path.exists())

    def test_cleanup_failure_does_not_fail_activation(self):
        old, new, workspace = self.candidate_workspace()
        r.accept_conflict(self.home, old, new, workspace, lambda _: "yes")
        plan = r.check(self.home, old, new)
        r.apply(self.home, plan)
        (workspace / "candidate").unlink()
        (workspace / "candidate").symlink_to(self.live)
        output = self.notice_output(lambda: r.finish_activation(self.home, plan))
        self.assertIn("cleanup skipped", output)
        self.assertTrue(workspace.exists())

    def test_cleanup_cli(self):
        old, new, _ = self.candidate_workspace()
        command = [sys.executable, r.__file__, "resolve", "--generation", new]
        env = dict(os.environ, HOME=str(self.home))
        result = subprocess.run(command + ["--cleanup", "--dry-run"], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for args in (["--replace", "config", "--dry-run"], ["--cleanup", "config"]):
            result = subprocess.run(command + args, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_candidate_declined_without_writes(self):
        old, new, workspace = self.candidate_workspace()
        before = r.snapshot(self.live)
        self.assertFalse(r.accept_conflict(self.home, old, new, workspace, lambda _: "no"))
        self.assertEqual(r.snapshot(self.live), before)
        self.assertFalse(r.approval_path(self.home, "config").exists())
        self.assertEqual(list(self.home.glob("config.hm-backup-*")), [])

    def test_candidate_stale_live_or_generation_refused(self):
        old, new, workspace = self.candidate_workspace()
        other = self.generation("other", b"size=40\n")
        with self.assertRaisesRegex(r.Divergence, "inputs changed"):
            r.accept_conflict(self.home, old, other, workspace, lambda _: self.fail("must not prompt"))
        self.live.write_bytes(b"new app changes\n")
        with self.assertRaisesRegex(r.Divergence, "inputs changed"):
            r.accept_conflict(self.home, old, new, workspace, lambda _: self.fail("must not prompt"))
        self.assertEqual(self.live.read_bytes(), b"new app changes\n")
        self.assertFalse(r.approval_path(self.home, "config").exists())

    def test_candidate_changed_during_review_refused(self):
        old, new, workspace = self.candidate_workspace()
        def confirm(_):
            (workspace / "candidate").write_bytes(b"not reviewed\n")
            return "yes"
        with self.assertRaisesRegex(r.Divergence, "changed during review"):
            r.accept_conflict(self.home, old, new, workspace, confirm)
        self.assertFalse(r.approval_path(self.home, "config").exists())
        self.assertEqual(list(self.home.glob("config.hm-backup-*")), [])

    def test_candidate_symlink_and_binary_refused(self):
        old, new, workspace = self.candidate_workspace()
        candidate = workspace / "candidate"
        candidate.unlink()
        candidate.symlink_to(self.live)
        with self.assertRaises(r.Divergence):
            r.accept_conflict(self.home, old, new, workspace, lambda _: "yes")
        candidate.unlink()
        candidate.write_bytes(b"bad\0bytes")
        with self.assertRaises(r.Divergence):
            r.accept_conflict(self.home, old, new, workspace, lambda _: "yes")
        self.assertFalse(r.approval_path(self.home, "config").exists())

    def test_candidate_cli_export_decline_and_accept(self):
        old = self.generation("old", b"size=20\n")
        new = self.generation("new", b"size=30\n")
        self.live.write_bytes(b"size=24\n")
        state_home = self.home / ".local/state"
        gcroot = state_home / "home-manager/gcroots/current-home"
        gcroot.parent.mkdir(parents=True)
        gcroot.symlink_to(old)
        command = [sys.executable, r.__file__, "resolve", "--generation", new]
        env = dict(os.environ, HOME=str(self.home), XDG_STATE_HOME=str(state_home))
        subprocess.run(command + ["--export", "config"], env=env, check=True, capture_output=True)
        workspace = next((state_home / "home-manager/reconciliation/workspaces").glob("conflict-*/"))
        (workspace / "candidate").write_bytes(b"size=30\n# retained\n")
        declined = subprocess.run(command + ["--accept", str(workspace)], env=env,
                                  input="no\n", text=True, capture_output=True, check=True)
        self.assertIn("Declined", declined.stdout)
        self.assertFalse(r.approval_path(self.home, "config").exists())
        accepted = subprocess.run(command + ["--accept", str(workspace)], env=env,
                                  input="yes\n", text=True, capture_output=True, check=True)
        self.assertIn("-size=24", accepted.stdout)
        self.assertIn("+size=30", accepted.stdout)
        self.assertEqual(self.live.read_bytes(), b"size=24\n")
        self.activate(old, new)
        self.assertEqual(self.live.read_bytes(), b"size=30\n# retained\n")

    def test_removed_merge_declared_cli_rejects_without_writes(self):
        old, new, workspace = self.candidate_workspace()
        before = r.snapshot(self.live)
        result = subprocess.run(
            [sys.executable, r.__file__, "resolve", "--generation", new,
             "--merge-declared", "config"],
            env=dict(os.environ, HOME=str(self.home)), capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(r.snapshot(self.live), before)
        self.assertFalse(r.approval_path(self.home, "config").exists())
        self.assertEqual(list(self.home.glob("config.hm-backup-*")), [])
        self.assertNotIn("--merge-declared", r.resolution_guidance(old, new, "config"))

    def test_old_merge_policy_approval_is_not_accepted(self):
        old = self.generation("old", b"size=15\n")
        new = self.generation("new", b"size=20\n")
        self.live.write_bytes(b"size=16\n")
        r.resolve(self.home, old, new, "config")
        receipt_path = r.approval_path(self.home, "config")
        receipt = json.loads(receipt_path.read_text())
        receipt.pop("policy")
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaises(r.Divergence):
            self.activate(old, new)


if __name__ == "__main__":
    unittest.main()
