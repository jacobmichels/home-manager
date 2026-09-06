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


if __name__ == "__main__":
    unittest.main()
