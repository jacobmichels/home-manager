"""Run with python3 -m unittest discover -s tests/modules/files -p '*_test.py'."""
import importlib.util
import json
import os
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

    def test_explicit_replace_and_merge_are_distinct(self):
        base = b"size=14\nseparator\ntheme=light\n"
        live = b"size=12\nseparator\ntheme=dark\n"
        desired = b"size=20\nseparator\ntheme=light\n"
        old = self.generation("old", base)
        new = self.generation("new", desired)
        for replace, expected in [(False, b"size=20\nseparator\ntheme=dark\n"),
                                  (True, desired)]:
            with self.subTest(replace=replace):
                self.live.write_bytes(live)
                r.resolve(self.home, old, new, "config", replace=replace)
                self.assertEqual(self.live.read_bytes(), live)
                receipt = json.loads(r.approval_path(self.home, "config").read_text())
                self.assertEqual(Path(receipt["backup"]).read_bytes(), live)
                self.activate(old, new)
                self.assertEqual(self.live.read_bytes(), expected)
                self.assertFalse(r.approval_path(self.home, "config").exists())
                # Consumed approval cannot silently resolve a later edit.
                self.live.write_bytes(live)
                with self.assertRaises(r.Divergence):
                    self.activate(old, new)

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

    def test_declared_preferred_merge_preserves_surrounding_additions(self):
        old = self.generation("old", b"theme=light\nfont-size=15\n")
        new = self.generation("new", b"theme=light\nfont-size=20\n")
        live = b"foo=bar\ntheme=light\nhello=world\nfont-size=16\nmeow=mix\n"
        self.live.write_bytes(live)
        r.resolve(self.home, old, new, "config")
        self.assertEqual(self.live.read_bytes(), live)
        receipt = json.loads(r.approval_path(self.home, "config").read_text())
        self.assertEqual(Path(receipt["backup"]).read_bytes(), live)
        self.activate(old, new)
        self.assertEqual(self.live.read_bytes(), live.replace(b"font-size=16", b"font-size=20"))

    def test_declared_preferred_merge_refuses_ambiguous_alignment(self):
        for base, live, desired in [
            (b"size=15\n", b"size=16\nsize=17\n", b"size=20\n"),
            (b"red\n", b"green\nextra\n", b"blue\n"),
            (b'"red",\n', b'"green",\nextra\n', b'"blue",\n'),
            (b"a\nb\n", b"A\nextra\nB\n", b"C\nD\n"),
            (b"anchor\n", b"left\nanchor\n", b"right\nanchor\n"),
        ]:
            with self.subTest(live=live), self.assertRaises(r.Divergence):
                r.merge(base, live, desired, prefer_declared=True)

    def test_explicit_replace_still_works_when_merge_is_ambiguous(self):
        old = self.generation("old", b"red\n")
        new = self.generation("new", b"blue\n")
        self.live.write_bytes(b"green\nextra\n")
        with self.assertRaises(r.Divergence):
            r.resolve(self.home, old, new, "config")
        self.assertFalse(r.approval_path(self.home, "config").exists())
        self.assertEqual(list(self.home.glob("config.hm-backup-*")), [])
        r.resolve(self.home, old, new, "config", replace=True)
        self.activate(old, new)
        self.assertEqual(self.live.read_bytes(), b"blue\n")


    def test_adjacent_declared_replacements_align_individually(self):
        self.assertEqual(r.merge(b"theme=light\nsize=15\n",
                                 b"theme=dark\nsize=16\n",
                                 b"theme=catppuccin\nsize=20\n", prefer_declared=True),
                         b"theme=catppuccin\nsize=20\n")

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
        for prefer_declared in (False, True):
            self.assertEqual(r.merge(base, live, desired, prefer_declared=prefer_declared),
                             b'#!/bin/sh\n# before\necho new\naudit\n')

    def test_boundary_insertion_exception_does_not_hide_ambiguity(self):
        for base, live, desired in (
            (b"a\nb\n", b"a\nextra\nb\n", b"a\n"),  # deletion
            (b"a\nb\n", b"extra\na\nb\n", b"A\nB\n"),  # block replacement
            (b"a\n", b"extra\na\n", b"other\na\n"),  # competing insertions
            (b"a\na\n", b"extra\na\na\n", b"A\na\n"),  # repeated anchor
            (b"a\nb\n", b"extra\na\nb\na\n", b"A\nb\n"),  # duplicated live anchor
        ):
            for prefer_declared in (False, True):
                with self.subTest(base=base, live=live, desired=desired,
                                  prefer_declared=prefer_declared):
                    with self.assertRaises(r.Divergence):
                        r.merge(base, live, desired, prefer_declared=prefer_declared)

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
        r.resolve(self.home, old, new, "config")
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
        # declared-preferred resolution must refuse ambiguous overlapping hunks.
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
                    r.resolve(self.home, old, new, "config")
                self.assertEqual(r.snapshot(self.live), before)
                self.assertFalse(r.approval_path(self.home, "config").exists())
                self.assertEqual(list(self.home.glob("config.hm-backup-*")), [])

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
