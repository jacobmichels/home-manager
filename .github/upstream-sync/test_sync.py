import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("sync", Path(__file__).with_name("sync.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        sync.git(self.repo, "init", "-b", "master")
        sync.git(self.repo, "config", "user.name", "Test")
        sync.git(self.repo, "config", "user.email", "test@example.com")
        self.commit("fish.nix", "original\n")
        self.common = sync.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.env = patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.root / "outputs")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def commit(self, file, content):
        (self.repo / file).write_text(content)
        sync.git(self.repo, "add", "--all")
        sync.git(self.repo, "commit", "-m", file)
        return sync.git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def divergent(self, conflict=False):
        sync.git(self.repo, "switch", "-c", "upstream")
        upstream = self.commit("fish.nix" if conflict else "upstream.nix", "upstream\n")
        sync.git(self.repo, "switch", "master")
        self.commit("fish.nix", "fork option\n")
        data = sync.prepare_merge(self.repo, upstream)
        sync.save(self.state, data)
        return data

    def result(self):
        return json.loads((self.state / "result.json").read_text())

    def test_no_change(self):
        self.assertEqual(sync.prepare_merge(self.repo, self.common)["status"], "unchanged")

    def test_clean_merge_bundle_preserves_both_parents_and_customization(self):
        data = self.divergent()
        self.assertEqual(data["status"], "merged")
        sync.finish(self.repo, self.state)
        self.assertEqual(self.result()["status"], "candidate")
        self.assertEqual(sync.git(self.repo, "show", "-s", "--format=%P", "HEAD").stdout.split(),
                         [data["base"], data["upstream"]])
        self.assertEqual((self.repo / "fish.nix").read_text(), "fork option\n")
        clone = self.root / "clone"
        sync.run("git", "clone", "--quiet", str(self.repo), str(clone))
        sync.git(clone, "reset", "--hard", data["base"])
        sync.git(clone, "bundle", "verify", str(self.state / "candidate.bundle"))
        sync.git(clone, "fetch", str(self.state / "candidate.bundle"), "HEAD")
        self.assertEqual(sync.git(clone, "rev-parse", "FETCH_HEAD").stdout.strip(), self.result()["head"])

    def test_conflict_without_api_escalates_without_committing(self):
        data = self.divergent(conflict=True)
        self.assertEqual(data["conflicts"], ["fish.nix"])
        sync.finish(self.repo, self.state)
        self.assertEqual(self.result()["status"], "blocked")
        self.assertEqual(sync.git(self.repo, "rev-parse", "HEAD").stdout.strip(), data["base"])
        self.assertFalse((self.state / "candidate.bundle").exists())

    def test_successful_agent_resolution(self):
        self.divergent(conflict=True)
        (self.repo / "fish.nix").write_text("fork option and upstream\n")
        (self.state / "codex.json").write_text(json.dumps({"resolved": True, "summary": "Kept both."}))
        sync.finish(self.repo, self.state)
        self.assertEqual(self.result()["status"], "candidate")

    def test_agent_needs_decision_even_if_files_were_edited(self):
        self.divergent(conflict=True)
        (self.repo / "fish.nix").write_text("tentative\n")
        (self.state / "codex.json").write_text(json.dumps({"resolved": False, "summary": "Which behavior?"}))
        sync.finish(self.repo, self.state)
        self.assertEqual(self.result()["status"], "blocked")
        self.assertEqual(self.result()["summary"], "Which behavior?")

    def test_agent_cannot_claim_success_with_remaining_conflict_markers(self):
        self.divergent(conflict=True)
        (self.state / "codex.json").write_text(json.dumps({"resolved": True, "summary": "Done"}))
        sync.finish(self.repo, self.state)
        self.assertEqual(self.result()["status"], "blocked")

    def test_changed_merge_parents_are_rejected(self):
        self.divergent()
        sync.git(self.repo, "merge", "--abort")
        sync.finish(self.repo, self.state)
        self.assertEqual(self.result()["status"], "blocked")

    def test_existing_pr_stops_preparation_before_fetch_or_edit(self):
        with patch.object(sync, "gh", return_value='[{"number": 1, "url": "https://example.com/pr/1"}]'), \
                patch.object(sync, "prepare_merge") as merge:
            sync.prepare(self.repo, self.state)
        merge.assert_not_called()
        self.assertEqual(self.result()["status"], "pending")

    def test_decision_issue_accepts_cli_bot_identity_but_not_other_authors(self):
        for login in ("app/github-actions", "github-actions[bot]", "someone-else"):
            with self.subTest(login=login):
                issue = {"number": 2, "title": sync.ISSUE_TITLE, "body": sync.MARKER,
                         "author": {"login": login}}
                with patch.object(sync, "gh", return_value=json.dumps([issue])):
                    self.assertEqual(sync.find_issue(), issue if login in sync.BOT_LOGINS else None)

    def test_failed_validation_creates_draft_and_preserves_body_newlines(self):
        data = self.divergent()
        sync.finish(self.repo, self.state)
        calls = []

        def fake_gh(*args):
            calls.append(args)
            if args[:2] in [("pr", "list"), ("issue", "list")]:
                return "[]"
            if args[:2] == ("pr", "create"):
                return "https://github.com/jacobmichels/home-manager/pull/1"
            if args[:2] == ("pr", "view"):
                return (self.state / "body.md").read_text()
            raise AssertionError(args)

        original_git = sync.git

        def fake_git(repo, *args, **kwargs):
            from subprocess import CompletedProcess
            if args[:2] == ("rev-parse", "FETCH_HEAD"):
                return CompletedProcess(args, 0, data["base"] + "\n", "")
            if args[0] in ("fetch", "push", "ls-remote"):
                return CompletedProcess(args, 0, "", "")
            return original_git(repo, *args, **kwargs)

        with patch.object(sync, "gh", side_effect=fake_gh), \
                patch.object(sync, "git", side_effect=fake_git), \
                patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "VALIDATION_RESULT": "failure"}):
            sync.publish(self.repo, self.state)
        create = next(args for args in calls if args[:2] == ("pr", "create"))
        self.assertIn("--draft", create)
        self.assertIn("--body-file", create)
        self.assertIn("\n\n", (self.state / "body.md").read_text())


if __name__ == "__main__":
    unittest.main()
