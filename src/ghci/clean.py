#!/usr/bin/env python3
"""ghclean: audit and clean up your GitHub PRs, forks and local clones.

Part of the ghci package (with ghci, ghci/actions.py). One file, stdlib only, importing nothing else from the package, so it
can be copied anywhere and run as a script. Needs `gh` (logged in: `gh auth login`) and git >= 2.38.

    ghclean prs                          your open PRs: age, CI, review, mergeable (--json for scripts)
    ghclean close octo/app#225 ... -m "why"          preview; add --yes to close (skips non-open / not yours)
    ghclean clones [--root ~]            clones under root: dirty files, unpushed branches, token in remote URL
    ghclean remotes [--fix]              remotes with credentials in the URL; --fix rewrites them to SSH
    ghclean branches ~/api ...         local + fork branches: merged / content-in-upstream / unmerged
    ghclean branches ~/api --delete-local --delete-fork --yes      delete the merged ones (sha log kept)
    ghclean preserve ~/web opsbot tagger [--skip-hook]             push laptop-only branches as preserve/<b>

What counts as merged: `git cherry` finds no commit missing from the upstream default branch, or merging the
branch into it changes nothing (squash-merged or re-done upstream). Never deleted: the default branch, branches
checked out in any worktree, heads of your open PRs, and --keep patterns (default: preserve/*, *backup*).
Every deleted branch is appended to ~/.ghclean-deleted.log as `<when> <repo> <local|fork> <branch> <sha>`;
restore with `git branch <b> <sha>` or `git push origin <sha>:refs/heads/<b>`.

Gotchas: a global pre-push hook (core.hooksPath) may rebase the checked-out branch on every push; `preserve`
pushes refs without checking them out, and only sets GIT_PUSH_HOOK_SKIP=1 with --skip-hook. Fork branches are
deleted through the GitHub API, not `git push --delete`, so no hook runs. A gh HTTP 401 stops the command
(run `gh auth login`); it is never treated as "PR not found".

Exit codes: 0 ok, 1 something failed, 2 usage / gh error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DELETE_LOG = Path.home() / ".ghclean-deleted.log"
DEFAULT_KEEP = ["preserve/*", "*backup*"]
CRED_URL = re.compile(r"^https://[^/@]+@github\.com/")


class GhError(Exception):
    pass


# ---------------------------------------------------------------- gh / git helpers

def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if "HTTP 401" in r.stderr:
        raise GhError("gh: HTTP 401 — the gh login is invalid; run `gh auth login` and retry")
    if check and r.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout


def gh_json(*args: str) -> Any:
    out = gh(*args)
    return json.loads(out) if out.strip() else None


def git(repo: Path, *args: str, check: bool = False) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip() if r.returncode == 0 else ""


def gh_slug(url: str) -> str | None:
    """owner/repo from any github.com remote URL (ssh or https, with or without credentials)."""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def mask(url: str) -> str:
    return CRED_URL.sub("https://***@github.com/", url)


def me() -> str:
    return gh("api", "user", "--jq", ".login").strip()


def log_delete(repo: str, where: str, branch: str, sha: str) -> None:
    with DELETE_LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} {repo} {where} {branch} {sha}\n")


def is_clone(p: Path) -> bool:
    return (p / ".git").exists()


# ---------------------------------------------------------------- prs

def pr_detail(repo: str, number: int) -> dict[str, Any]:
    d = gh_json("pr", "view", str(number), "-R", repo, "--json",
                "number,title,isDraft,reviewDecision,mergeable,statusCheckRollup,headRefName,updatedAt,"
                "createdAt,additions,deletions,changedFiles,url")
    checks = [c.get("conclusion") or c.get("state") or "?" for c in d.get("statusCheckRollup") or []]
    if not checks:
        ci = "none"
    elif any(c in ("FAILURE", "ERROR", "TIMED_OUT") for c in checks):
        ci = "FAIL"
    elif all(c in ("SUCCESS", "SKIPPED", "NEUTRAL") for c in checks):
        ci = "pass"
    else:
        ci = "pending"
    updated = datetime.fromisoformat(d["updatedAt"].replace("Z", "+00:00"))
    return {"repo": repo, "number": d["number"], "title": d["title"], "draft": d["isDraft"],
            "review": d.get("reviewDecision") or "-", "mergeable": d["mergeable"], "ci": ci,
            "head": d["headRefName"], "idle_days": (datetime.now(timezone.utc) - updated).days,
            "size": f"+{d['additions']}/-{d['deletions']} {d['changedFiles']}f", "url": d["url"]}


def cmd_prs(a: argparse.Namespace) -> int:
    author = a.author or me()
    hits = gh_json("search", "prs", "--author", author, "--state", "open", "--limit", "300",
                   "--json", "repository,number") or []
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(lambda h: pr_detail(h["repository"]["nameWithOwner"], h["number"]), hits))
    rows.sort(key=lambda r: (r["repo"], -r["idle_days"]))
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print(f"{r['repo'] + '#' + str(r['number']):<34} {r['idle_days']:>4}d {r['ci']:<7} {r['mergeable']:<11} "
              f"{r['review']:<17} {'draft ' if r['draft'] else ''}{r['title'][:70]}")
    print(f"{len(rows)} open PRs by {author}")
    return 0


# ---------------------------------------------------------------- close

def parse_ref(ref: str) -> tuple[str, int]:
    m = re.fullmatch(r"(?:https://github\.com/)?([^/#\s]+/[^/#\s]+?)(?:#|/pull/)(\d+)", ref.strip())
    if not m:
        raise ValueError(f"not a PR reference (owner/repo#N or PR URL): {ref}")
    return m.group(1), int(m.group(2))


def cmd_close(a: argparse.Namespace) -> int:
    refs = list(a.prs) or [line for line in sys.stdin.read().split() if line]
    user = me()
    failed = 0
    for ref in refs:
        repo, n = parse_ref(ref)
        d = gh_json("pr", "view", str(n), "-R", repo, "--json", "state,author,title")
        if d["state"] != "OPEN":
            print(f"skip   {repo}#{n} ({d['state'].lower()})")
            continue
        if d["author"]["login"] != user and not a.any_author:
            print(f"skip   {repo}#{n} (author {d['author']['login']}, not you; --any-author to override)")
            continue
        if not a.yes:
            print(f"would close {repo}#{n} {d['title'][:70]}")
            continue
        r = subprocess.run(["gh", "pr", "close", str(n), "-R", repo, "-c", a.message],
                           capture_output=True, text=True, check=False)
        if "HTTP 401" in r.stderr:
            raise GhError("gh: HTTP 401 — run `gh auth login` and rerun (closed PRs are skipped)")
        print(f"{'closed' if r.returncode == 0 else 'FAILED'} {repo}#{n}")
        failed += r.returncode != 0
    if not a.yes:
        print("(preview only; add --yes to close)")
    return 1 if failed else 0


# ---------------------------------------------------------------- clones / remotes

def clones_under(root: Path) -> list[Path]:
    found = []
    for p in root.iterdir():
        try:
            if p.is_dir() and is_clone(p):
                found.append(p)
        except OSError:  # macOS-protected folders under ~ (PermissionError)
            continue
    return sorted(found)


def cmd_clones(a: argparse.Namespace) -> int:
    for repo in clones_under(Path(a.root).expanduser()):
        remotes = git(repo, "remote").split()
        urls = {r: git(repo, "remote", "get-url", r) for r in remotes}
        if a.github_only and not any(gh_slug(u) for u in urls.values()):
            continue
        dirty = len([line for line in git(repo, "status", "--porcelain").splitlines() if line])
        refs = git(repo, "for-each-ref", "--format=%(refname:short)|%(upstream:short)|%(upstream:track)",
                   "refs/heads").splitlines()
        no_up = sum(1 for r in refs if r.split("|")[1] == "")
        ahead = sum(1 for r in refs if "ahead" in r.split("|")[2])
        token = any(CRED_URL.match(u) for u in urls.values())
        print(f"{repo.name:<24} {git(repo, 'branch', '--show-current') or '(detached)':<34} dirty={dirty:<4} "
              f"branches={len(refs):<3} no-upstream={no_up:<3} ahead={ahead:<3} last={git(repo, 'log', '-1', '--format=%cs')}"
              f"{'  TOKEN-IN-REMOTE' if token else ''}  "
              + " ".join(f"{r}={gh_slug(u) or mask(u)}" for r, u in urls.items()))
    return 0


def cmd_remotes(a: argparse.Namespace) -> int:
    found = 0
    for repo in clones_under(Path(a.root).expanduser()):
        for r in git(repo, "remote").split():
            for kind, url in (("fetch", git(repo, "remote", "get-url", r)),
                              ("push", git(repo, "config", "--get", f"remote.{r}.pushurl"))):
                if not url or not CRED_URL.match(url):
                    continue
                found += 1
                slug = gh_slug(url)
                if not slug:
                    print(f"{repo.name} {r} ({kind}) {mask(url)}: not owner/repo, fix by hand")
                    continue
                new = f"git@github.com:{slug}.git"
                if a.fix:
                    if kind == "push":
                        git(repo, "config", f"remote.{r}.pushurl", new, check=True)
                    else:
                        git(repo, "remote", "set-url", r, new, check=True)
                print(f"{repo.name} {r} ({kind}) {mask(url)} -> {new}{'' if a.fix else '  (preview; --fix)'}")
    print(f"{found} remote URL(s) with credentials" + (", rewritten" if a.fix and found else ""))
    return 0


# ---------------------------------------------------------------- branches

def contained(repo: Path, base: str, ref: str) -> str | None:
    """'merged' / 'content-in' if ref adds nothing to base, else None (also when git fails: never guess merged)."""
    r = subprocess.run(["git", "-C", str(repo), "cherry", base, ref], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    if not any(line.startswith("+") for line in r.stdout.splitlines()):
        return "merged"
    tree = git(repo, "merge-tree", "--write-tree", base, ref).split("\n", 1)[0]
    if tree and tree == git(repo, "rev-parse", f"{base}^{{tree}}"):
        return "content-in"
    return None


def branch_report(repo: Path, keep_patterns: list[str]) -> dict[str, Any]:
    remotes = git(repo, "remote").split()
    up = "upstream" if "upstream" in remotes else "origin"
    up_slug = gh_slug(git(repo, "remote", "get-url", up))
    fork_slug = gh_slug(git(repo, "remote", "get-url", "origin")) if "origin" in remotes and up != "origin" else None
    if not up_slug:
        raise GhError(f"{repo.name}: {up} is not a GitHub remote")
    git(repo, "fetch", "-q", up)
    if fork_slug:
        git(repo, "fetch", "-q", "origin")
    default = gh("repo", "view", up_slug, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name").strip()
    base = f"{up}/{default}"
    if not git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/{base}^{{commit}}"):
        raise GhError(f"{repo.name}: no {base} ref (fetch {up} failed?); nothing classified")
    owner = fork_slug.split("/")[0] if fork_slug else None
    open_heads: set[str] = set()
    for pr in gh_json("pr", "list", "-R", up_slug, "--state", "open", "--limit", "300", "--json",
                      "headRefName,headRepositoryOwner") or []:
        if owner is None or (pr.get("headRepositoryOwner") or {}).get("login") == owner:
            open_heads.add(pr["headRefName"])
    if fork_slug:
        open_heads |= {p["headRefName"] for p in gh_json("pr", "list", "-R", fork_slug, "--state", "open",
                                                          "--json", "headRefName") or []}
    checked_out = {line.split(" ", 1)[1].removeprefix("refs/heads/")
                   for line in git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("branch ")}

    def protected(b: str) -> str | None:
        if b == default:
            return "default"
        if b in open_heads:
            return "open-PR"
        if any(fnmatch.fnmatch(b, p) for p in keep_patterns):
            return "keep"
        return None

    local = []
    for line in git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines():
        b, sha = line.split()
        on_fork = git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}") if fork_slug else ""
        status = protected(b) or ("checked-out" if b in checked_out else None)
        local.append({"branch": b, "sha": sha, "merged": contained(repo, base, sha), "protected": status,
                      "on_fork": "same" if on_fork == sha else ("differs" if on_fork else "no"),
                      "last": git(repo, "log", "-1", "--format=%cs", sha)})
    fork = []
    if fork_slug:
        # --paginate prints one JSON array per page, so read name/sha lines rather than one JSON document
        for line in gh("api", f"repos/{fork_slug}/branches", "--paginate",
                       "--jq", '.[] | .name + " " + .commit.sha').splitlines():
            if not line.strip():
                continue
            b, sha = line.rsplit(" ", 1)
            known = bool(git(repo, "cat-file", "-t", sha))
            fork.append({"branch": b, "sha": sha, "protected": protected(b),
                         "merged": contained(repo, base, sha) if known else None, "known": known})
    return {"repo": repo, "base": base, "fork": fork_slug, "local": local, "fork_branches": fork}


def cmd_branches(a: argparse.Namespace) -> int:
    keep = DEFAULT_KEEP + (a.keep or [])
    failed = 0
    for d in a.clones:
        repo = Path(d).expanduser()
        rep = branch_report(repo, keep)
        print(f"== {repo.name}  (base {rep['base']}, fork {rep['fork'] or '-'})")
        del_local = [b for b in rep["local"] if b["merged"] and not b["protected"]]
        del_fork = [b for b in rep["fork_branches"] if b["merged"] and not b["protected"]]
        for b in rep["local"]:
            if not b["merged"]:
                print(f"  unmerged  {b['branch']:<45} {b['last']}  on-fork={b['on_fork']}"
                      f"{'  [' + b['protected'] + ']' if b['protected'] else ''}")
        print(f"  local: {len(rep['local'])} branches, {len(del_local)} deletable"
              + (f"; fork: {len(rep['fork_branches'])} branches, {len(del_fork)} deletable" if rep["fork"] else ""))
        if a.verbose:
            for b in del_local:
                print(f"    local {b['branch']} ({b['merged']})")
            for b in del_fork:
                print(f"    fork  {b['branch']} ({b['merged']})")
        if a.delete_local:
            for b in del_local:
                if not a.yes:
                    print(f"  would delete local {b['branch']}")
                    continue
                ok = subprocess.run(["git", "-C", str(repo), "branch", "-D", b["branch"]],
                                    capture_output=True, check=False).returncode == 0
                if ok:
                    log_delete(repo.name, "local", b["branch"], b["sha"])
                print(f"  {'deleted' if ok else 'FAILED'} local {b['branch']} {b['sha'][:8]}")
                failed += not ok
        if a.delete_fork and rep["fork"]:
            for b in del_fork:
                if not a.yes:
                    print(f"  would delete fork {b['branch']}")
                    continue
                now = gh("api", f"repos/{rep['fork']}/git/ref/heads/{b['branch']}", "--jq", ".object.sha",
                         check=False).strip()
                if now != b["sha"]:
                    print(f"  skip fork {b['branch']} (moved since check)")
                    continue
                ok = subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{rep['fork']}/git/refs/heads/{b['branch']}"],
                                    capture_output=True, check=False).returncode == 0
                if ok:
                    log_delete(rep["fork"], "fork", b["branch"], b["sha"])
                print(f"  {'deleted' if ok else 'FAILED'} fork {b['branch']} {b['sha'][:8]}")
                failed += not ok
        if (a.delete_local or a.delete_fork) and not a.yes:
            print("  (preview only; add --yes to delete)")
    return 1 if failed else 0


# ---------------------------------------------------------------- preserve

def cmd_preserve(a: argparse.Namespace) -> int:
    repo = Path(a.clone).expanduser()
    env = dict(os.environ, GIT_PUSH_HOOK_SKIP="1") if a.skip_hook else None
    failed = 0
    for b in a.branches:
        sha = git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{b}")
        if not sha:
            print(f"no local branch {b}")
            failed += 1
            continue
        dst = f"{a.prefix}{b}"
        r = subprocess.run(["git", "-C", str(repo), "push", "-q", a.remote, f"refs/heads/{b}:refs/heads/{dst}"],
                           capture_output=True, text=True, env=env, check=False)
        remote_sha = git(repo, "ls-remote", a.remote, f"refs/heads/{dst}").split("\t")[0]
        ok = r.returncode == 0 and remote_sha == sha
        print(f"{'pushed' if ok else 'FAILED'} {b} -> {a.remote}/{dst} {sha[:8]}"
              + ("" if ok else f"  {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}"))
        failed += not ok
    return 1 if failed else 0


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ghclean", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("prs", help="your open PRs with age, CI, review, mergeable")
    s.add_argument("--author", help="GitHub login (default: you)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_prs)

    s = sub.add_parser("close", help="close PRs with a comment (preview unless --yes)")
    s.add_argument("prs", nargs="*", help="owner/repo#N or PR URLs (else read from stdin)")
    s.add_argument("-m", "--message", required=True, help="comment left on each PR")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--any-author", action="store_true", help="also close PRs you did not open")
    s.set_defaults(fn=cmd_close)

    s = sub.add_parser("clones", help="survey git clones under a directory")
    s.add_argument("--root", default="~")
    s.add_argument("--github-only", action="store_true")
    s.set_defaults(fn=cmd_clones)

    s = sub.add_parser("remotes", help="find (and --fix) remote URLs carrying credentials")
    s.add_argument("--root", default="~")
    s.add_argument("--fix", action="store_true", help="rewrite them to git@github.com:owner/repo.git")
    s.set_defaults(fn=cmd_remotes)

    s = sub.add_parser("branches", help="classify (and delete merged) local + fork branches")
    s.add_argument("clones", nargs="+", help="clone directories (origin = your fork, upstream = the org repo)")
    s.add_argument("--keep", action="append", help=f"glob never deleted (always: {', '.join(DEFAULT_KEEP)})")
    s.add_argument("--delete-local", action="store_true")
    s.add_argument("--delete-fork", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="list the deletable branches")
    s.set_defaults(fn=cmd_branches)

    s = sub.add_parser("preserve", help="push local-only branches to the fork as preserve/<branch>")
    s.add_argument("clone")
    s.add_argument("branches", nargs="+")
    s.add_argument("--remote", default="origin")
    s.add_argument("--prefix", default="preserve/")
    s.add_argument("--skip-hook", action="store_true", help="set GIT_PUSH_HOOK_SKIP=1 for these pushes")
    s.set_defaults(fn=cmd_preserve)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (GhError, ValueError) as e:
        print(f"ghclean: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
