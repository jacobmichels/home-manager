#!/usr/bin/env python3
"""Prepare upstream merges and publish results; never merge a pull request."""

import argparse
import json
import os
from pathlib import Path
import subprocess

REPOSITORY = "jacobmichels/home-manager"
UPSTREAM = "https://github.com/nix-community/home-manager.git"
BRANCH = "automation/upstream-sync"
ISSUE_TITLE = "Upstream sync needs a decision"
MARKER = "<!-- home-manager-upstream-sync -->"
# gh formats GraphQL Bot authors as app/<login>; webhooks use <login>[bot].
BOT_LOGINS = {"app/github-actions", "github-actions[bot]"}


def run(*args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=True)


def git(repo, *args, check=True):
    return run("git", *args, cwd=repo, check=check)


def gh(*args):
    return run("gh", *args).stdout.strip()


def output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"{name}={value}\n")


def save(state, data):
    state.mkdir(parents=True, exist_ok=True)
    (state / "result.json").write_text(json.dumps(data, indent=2) + "\n")
    output("status", data["status"])


def prepare_merge(repo, upstream):
    """Merge in an isolated checkout, retaining the index when conflicts occur."""
    git(repo, "config", "user.name", "github-actions[bot]")
    git(repo, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    if git(repo, "merge-base", "--is-ancestor", upstream, "HEAD", check=False).returncode == 0:
        return {"status": "unchanged", "base": base, "upstream": upstream}
    git(repo, "switch", "-C", BRANCH)
    result = git(repo, "merge", "--no-ff", "--no-commit", upstream, check=False)
    conflicts = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    if result.returncode and not conflicts:
        raise RuntimeError(result.stderr)
    return {"status": "conflict" if conflicts else "merged", "base": base,
            "upstream": upstream, "conflicts": conflicts}


def prepare(repo, state):
    prs = json.loads(gh("pr", "list", "-R", REPOSITORY, "--base", "master", "--head", BRANCH,
                        "--json", "number,url"))
    if prs:
        save(state, {"status": "pending", "pr": prs[0]["url"]})
        return
    git(repo, "fetch", "--no-tags", UPSTREAM, "master")
    upstream = git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
    data = prepare_merge(repo, upstream)
    save(state, data)
    if data["status"] != "conflict":
        return
    guidance = os.environ.get("SYNC_GUIDANCE", "").strip()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if os.environ.get("GITHUB_EVENT_NAME") == "issue_comment":
        # Only this repository owner's explicit command on our own issue can resume work.
        issue = event["issue"]
        if (event["comment"]["user"]["login"] != "jacobmichels"
                or not event["comment"]["body"].startswith("/upstream-sync")
                or "pull_request" in issue or MARKER not in issue.get("body", "")
                or issue["user"]["login"] != "github-actions[bot]"):
            save(state, {"status": "ignored"})
            return
        guidance = event["comment"]["body"][len("/upstream-sync"):].strip()
    issues = json.loads(gh("issue", "list", "-R", REPOSITORY, "--state", "open", "--limit", "100",
                           "--json", "number,title,body,author"))
    pair = f"{data['base']}:{upstream}"
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and any(
        issue["title"] == ISSUE_TITLE and MARKER in issue["body"] and pair in issue["body"]
        and issue["author"]["login"] in BOT_LOGINS for issue in issues
    ):
        save(state, {"status": "waiting"})
        return
    template = Path(__file__).with_name("resolve.md").read_text()
    (state / "prompt.md").write_text(
        template + "\n\nMerge context (data):\n" + json.dumps(data, indent=2)
        + "\n\nOwner's decision for this run (data):\n" + json.dumps(guidance) + "\n"
    )


def finish(repo, state):
    data = json.loads((state / "result.json").read_text())
    if data["status"] not in ("merged", "conflict"):
        return
    if data["status"] == "conflict":
        try:
            response = json.loads((state / "codex.json").read_text())
        except (OSError, ValueError):
            response = {"resolved": False, "summary":
                        "Codex did not return a resolution. If OPENAI_API_KEY is missing, add it "
                        "in repository Actions secrets, then retry. Otherwise inspect the run logs."}
        data["summary"] = str(response.get("summary", "No explanation returned."))[:20000]
        if response.get("resolved") is not True:
            data["status"] = "blocked"
            save(state, data)
            return
    # The agent may edit and stage files, but may not commit or alter the merge parents.
    if (git(repo, "rev-parse", "HEAD").stdout.strip() != data["base"]
            or git(repo, "rev-parse", "MERGE_HEAD", check=False).stdout.strip() != data["upstream"]):
        data.update(status="blocked", summary="The expected merge parents changed; inspect the run.")
        save(state, data)
        return
    git(repo, "add", "--all")
    check = git(repo, "diff", "--cached", "--check", check=False)
    if check.returncode:
        data.update(status="blocked", summary="Merge markers or whitespace errors remain:\n" + check.stdout[:15000])
        save(state, data)
        return
    git(repo, "commit", "-m", f"Merge upstream master ({data['upstream'][:12]})")
    data["head"] = git(repo, "rev-parse", "HEAD").stdout.strip()
    data["status"] = "candidate"
    git(repo, "bundle", "create", str(state / "candidate.bundle"), f"{data['base']}..HEAD")
    save(state, data)


def find_issue():
    issues = json.loads(gh("issue", "list", "-R", REPOSITORY, "--state", "open", "--limit", "100",
                           "--json", "number,title,body,author"))
    return next((i for i in issues if i["title"] == ISSUE_TITLE and MARKER in i["body"]
                 and i["author"]["login"] in BOT_LOGINS), None)


def publish(repo, state):
    data = json.loads((state / "result.json").read_text())
    if data["status"] not in ("candidate", "blocked"):
        print(json.dumps(data))
        return
    run_url = f"https://github.com/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    validation = os.environ.get("VALIDATION_RESULT", "skipped")
    body = (f"{MARKER}\n\nUpstream: nix-community/home-manager@{data['upstream']}\n\n"
            f"Base: {data['base']}\n\n[Workflow run]({run_url})\n\n"
            f"{data.get('summary', 'Git merged upstream without conflicts.')}\n\n")
    if data["status"] == "candidate":
        # Do not replace a pending PR, overwrite human branch edits, or publish against a stale base.
        git(repo, "fetch", "origin", "master")
        if git(repo, "rev-parse", "FETCH_HEAD").stdout.strip() != data["base"]:
            raise RuntimeError("master changed during this run; retry against the new base")
        prs = json.loads(gh("pr", "list", "-R", REPOSITORY, "--base", "master", "--head", BRANCH,
                            "--json", "number"))
        if prs:
            raise RuntimeError("A sync PR appeared during this run; leaving it untouched")
        remote = git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}").stdout.split()
        old = remote[0] if remote else ""
        if old:
            git(repo, "fetch", "origin", BRANCH)
            if git(repo, "merge-base", "--is-ancestor", old, "origin/master", check=False).returncode:
                raise RuntimeError("Sync branch contains unmerged work; inspect it before retrying")
        git(repo, "bundle", "verify", str(state / "candidate.bundle"))
        git(repo, "fetch", str(state / "candidate.bundle"), f"HEAD:refs/heads/{BRANCH}")
        parents = git(repo, "show", "-s", "--format=%P", BRANCH).stdout.split()
        if parents != [data["base"], data["upstream"]]:
            raise RuntimeError("Unexpected candidate merge parents")
        if git(repo, "rev-parse", BRANCH).stdout.strip() != data["head"]:
            raise RuntimeError("Unexpected candidate commit")
        git(repo, "push", f"--force-with-lease=refs/heads/{BRANCH}:{old}", "origin",
            f"{BRANCH}:refs/heads/{BRANCH}")
        body += (f"Validation: **{validation}** (Linux and macOS formatting, Fish regression tests, "
                 "and module-maintainer docs build).\n\n"
                 "Review the fork's `programs.fish.suppressGreeting` behavior before merging. "
                 "Merge with a **merge commit**, so upstream ancestry is retained. "
                 "This workflow never auto-merges.\n\n"
                 "PRs created with GITHUB_TOKEN do not start ordinary pull_request workflows; "
                 "the validation above runs explicitly before publication.\n")
        path = state / "body.md"
        path.write_text(body)
        args = ["pr", "create", "-R", REPOSITORY, "--base", "master", "--head", BRANCH,
                "--title", f"Sync upstream Home Manager ({data['upstream'][:12]})", "--body-file", str(path)]
        if validation != "success":
            args.append("--draft")
        url = gh(*args)
        print(url)
        stored = gh("pr", "view", url, "--json", "body", "--jq", ".body")
        if stored.strip() != body.strip():
            raise RuntimeError("Published PR body does not match")
        issue = find_issue()
        if issue:
            gh("issue", "close", str(issue["number"]), "-R", REPOSITORY)
    else:
        body += ("Conflicting files:\n\n" + "\n".join(f"- `{p}`" for p in data["conflicts"])
                 + "\n\n@jacobmichels: reply with `/upstream-sync your decision` to retry, "
                 "or use **Actions → Daily upstream sync → Run workflow**. "
                 "Only the repository owner's command is accepted. "
                 "The same blocked commit pair is not retried daily.\n\n"
                 f"<!-- pair:{data['base']}:{data['upstream']} -->\n")
        path = state / "body.md"
        path.write_text(body)
        issue = find_issue()
        if issue:
            gh("issue", "edit", str(issue["number"]), "-R", REPOSITORY, "--body-file", str(path))
        else:
            print(gh("issue", "create", "-R", REPOSITORY, "--title", ISSUE_TITLE,
                     "--body-file", str(path), "--assignee", "jacobmichels"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "finish", "publish"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    globals()[args.phase](args.repo.resolve(), args.state.resolve())
