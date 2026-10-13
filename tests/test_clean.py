"""ghci.clean (ghclean): pure helpers, branch classification on throwaway git repos (tmp_path, a local bare repo
as the remote, no network), and the destructive modes (close, --delete-fork, remotes --fix) with gh mocked."""
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghci import clean as g

KIT = Path(g.__file__).parent


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "core.hooksPath=/dev/null", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, name, text=None):
    (repo / name).write_text(text or name)
    sh(repo, "add", name)
    sh(repo, "commit", "-qm", name)
    return sh(repo, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """origin = a bare repo reached through https://github.com/org/repo.git (insteadOf), main pushed."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("core.hooksPath", "/dev/null"),
                 (f"url.{bare}.insteadOf", "https://github.com/org/repo.git")):
        sh(repo, "config", k, v)
    sh(repo, "remote", "add", "origin", "https://github.com/org/repo.git")
    commit(repo, "base")
    sh(repo, "push", "-q", "origin", "main")
    monkeypatch.setattr(g, "DELETE_LOG", tmp_path / "deleted.log")
    monkeypatch.setattr(g, "gh_slug", lambda u: "org/repo" if u.endswith("remote.git") else g.__dict__["_real_slug"](u))
    return repo


g._real_slug = g.gh_slug


def fake_gh(monkeypatch, open_heads=(), fork_branches=()):
    calls = []

    def gh(*args, check=True):
        calls.append(args)
        if args[:2] == ("repo", "view"):
            return "main\n"
        if args[:2] == ("pr", "list"):
            return json.dumps([{"headRefName": h, "headRepositoryOwner": {"login": "me"}} for h in open_heads])
        if args[0] == "api" and args[1].endswith("/branches"):
            return "".join(f"{b} {s}\n" for b, s in fork_branches)
        raise AssertionError(args)

    monkeypatch.setattr(g, "gh", gh)
    return calls


# ---------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("url,slug", [
    ("git@github.com:octo/app.git", "octo/app"),
    ("https://github.com/octo/app.git", "octo/app"),
    ("https://github.com/octo/app", "octo/app"),
    ("https://x-access-token:ghp_abc@github.com/octo/api.git", "octo/api"),
    ("ssh://git@github.com/o/r.git/", "o/r"),
    ("https://gitlab.com/o/r.git", None),
    ("/tmp/remote.git", None),
])
def test_gh_slug(url, slug):
    assert g._real_slug(url) == slug


def test_mask_hides_credentials_only():
    assert g.mask("https://user:tok@github.com/o/r.git") == "https://***@github.com/o/r.git"
    assert g.mask("git@github.com:o/r.git") == "git@github.com:o/r.git"


@pytest.mark.parametrize("ref,out", [
    ("octo/app#225", ("octo/app", 225)),
    ("https://github.com/octo/web/pull/1247", ("octo/web", 1247)),
    (" o/r.js#3 ", ("o/r.js", 3)),
])
def test_parse_ref(ref, out):
    assert g.parse_ref(ref) == out


@pytest.mark.parametrize("bad", ["app#225", "octo/app", "octo/app#x", "https://github.com/o/r/issues/3"])
def test_parse_ref_rejects(bad):
    with pytest.raises(ValueError):
        g.parse_ref(bad)


def test_ghclean_is_one_standalone_stdlib_file():
    import sys
    text = (KIT / "clean.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(ghci|\.)", text, re.M)
    mods = {m.split(".")[0] for m in re.findall(r"^(?:from|import)\s+([\w.]+)", text, re.M)}
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib:
        assert mods - set(stdlib) - {"__future__"} == set()


def test_pr_detail_ci_rollup(monkeypatch):
    def detail(checks):
        monkeypatch.setattr(g, "gh_json", lambda *a: {
            "number": 1, "title": "t", "isDraft": False, "reviewDecision": None, "mergeable": "MERGEABLE",
            "statusCheckRollup": checks, "headRefName": "b", "updatedAt": "2026-01-01T00:00:00Z",
            "createdAt": "2026-01-01T00:00:00Z", "additions": 1, "deletions": 2, "changedFiles": 3, "url": "u"})
        return g.pr_detail("o/r", 1)
    assert detail([])["ci"] == "none"
    assert detail([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}])["ci"] == "pass"
    assert detail([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}])["ci"] == "FAIL"
    assert detail([{"conclusion": "SUCCESS"}, {"state": "PENDING"}])["ci"] == "pending"
    assert detail([])["review"] == "-" and detail([])["size"] == "+1/-2 3f"


def test_gh_401_is_an_error_not_empty(monkeypatch):
    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="HTTP 401: Bad credentials"))
    with pytest.raises(g.GhError, match="gh auth login"):
        g.gh("pr", "view", "1", check=False)


def test_clones_under_skips_unreadable(tmp_path, monkeypatch):
    (tmp_path / "a" / ".git").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    (tmp_path / "f").write_text("")
    assert [p.name for p in g.clones_under(tmp_path)] == ["a"]
    real = g.is_clone
    monkeypatch.setattr(g, "is_clone", lambda p: (_ for _ in ()).throw(PermissionError()) if p.name == "b" else real(p))
    assert [p.name for p in g.clones_under(tmp_path)] == ["a"]


# ---------------------------------------------------------------- contained / branch_report (real git)

def test_contained_merged_content_in_unmerged_and_git_failure(clone):
    sh(clone, "checkout", "-qb", "squashed")
    s = commit(clone, "s1")
    commit(clone, "s2")
    s_tip = sh(clone, "rev-parse", "HEAD")
    sh(clone, "checkout", "-q", "main")
    sh(clone, "merge", "-q", "--squash", "squashed")
    sh(clone, "commit", "-qm", "squash")
    sh(clone, "push", "-q", "origin", "main")
    sh(clone, "fetch", "-q", "origin")
    sh(clone, "checkout", "-qb", "unmerged")
    u = commit(clone, "u1")
    sh(clone, "checkout", "-q", "main")
    assert g.contained(clone, "origin/main", "main") == "merged"
    assert g.contained(clone, "origin/main", s_tip) == "content-in"
    assert g.contained(clone, "origin/main", s) == "content-in"
    assert g.contained(clone, "origin/main", u) is None
    # a missing base must never read as "merged" (git cherry fails with no output)
    assert g.contained(clone, "origin/nosuch", u) is None


def setup_branches(clone):
    sh(clone, "branch", "merged-ff")
    for b in ("preserve/old", "my-backup", "openpr", "keepme", "wtbranch"):
        sh(clone, "branch", b)
    sh(clone, "checkout", "-qb", "unmerged")
    commit(clone, "u1")
    sh(clone, "checkout", "-q", "main")
    sh(clone, "worktree", "add", "-q", str(clone.parent / "wt"), "wtbranch")


def test_branch_report_protections(clone, monkeypatch):
    setup_branches(clone)
    fake_gh(monkeypatch, open_heads=["openpr"])
    rep = g.branch_report(clone, g.DEFAULT_KEEP + ["keepme"])
    by = {b["branch"]: b for b in rep["local"]}
    assert {b: (v["merged"], v["protected"]) for b, v in by.items()} == {
        "main": ("merged", "default"), "merged-ff": ("merged", None), "preserve/old": ("merged", "keep"),
        "my-backup": ("merged", "keep"), "openpr": ("merged", "open-PR"), "keepme": ("merged", "keep"),
        "wtbranch": ("merged", "checked-out"), "unmerged": (None, None)}


def test_branch_report_refuses_without_base_ref(clone, monkeypatch):
    setup_branches(clone)
    fake_gh(monkeypatch)
    sh(clone, "update-ref", "-d", "refs/remotes/origin/main")
    sh(clone, "config", "--unset", f"url.{clone.parent / 'remote.git'}.insteadOf")
    sh(clone, "remote", "set-url", "origin", str(clone.parent / "gone.git"))
    monkeypatch.setattr(g, "gh_slug", lambda u: "org/repo")
    with pytest.raises(g.GhError, match="no origin/main ref"):
        g.branch_report(clone, g.DEFAULT_KEEP)


def branches_args(clone, **kw):
    d = dict(clones=[str(clone)], keep=None, delete_local=False, delete_fork=False, yes=False, verbose=False)
    d.update(kw)
    return SimpleNamespace(**d)


def test_delete_local_preview_then_yes_deletes_only_merged_and_logs_sha(clone, monkeypatch, capsys):
    setup_branches(clone)
    fake_gh(monkeypatch, open_heads=["openpr"])
    sha = sh(clone, "rev-parse", "merged-ff")
    assert g.cmd_branches(branches_args(clone, delete_local=True, keep=["keepme"])) == 0
    assert "would delete local merged-ff" in capsys.readouterr().out
    assert sh(clone, "rev-parse", "merged-ff") == sha
    assert not g.DELETE_LOG.exists()

    assert g.cmd_branches(branches_args(clone, delete_local=True, yes=True, keep=["keepme"])) == 0
    assert f"deleted local merged-ff {sha[:8]}" in capsys.readouterr().out
    left = set(sh(clone, "branch", "--format=%(refname:short)").split())
    assert left == {"main", "preserve/old", "my-backup", "openpr", "keepme", "wtbranch", "unmerged"}
    log = g.DELETE_LOG.read_text().split()
    assert log[1:] == ["clone", "local", "merged-ff", sha]


# ---------------------------------------------------------------- destructive gh paths, mocked

def test_delete_fork_checks_sha_then_deletes_via_api(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(g, "DELETE_LOG", tmp_path / "deleted.log")
    rep = {"repo": tmp_path, "base": "upstream/main", "fork": "me/repo", "local": [], "fork_branches": [
        {"branch": "done", "sha": "a" * 40, "merged": "merged", "protected": None, "known": True},
        {"branch": "moved", "sha": "b" * 40, "merged": "merged", "protected": None, "known": True},
        {"branch": "preserve/x", "sha": "c" * 40, "merged": "merged", "protected": "keep", "known": True},
        {"branch": "wip", "sha": "d" * 40, "merged": None, "protected": None, "known": True}]}
    monkeypatch.setattr(g, "branch_report", lambda repo, keep: rep)
    now = {"done": "a" * 40, "moved": "e" * 40}
    monkeypatch.setattr(g, "gh", lambda *a, check=True: now[a[1].rsplit("/", 1)[1]] + "\n")
    deleted = []
    monkeypatch.setattr(g.subprocess, "run", lambda cmd, **k: deleted.append(cmd) or SimpleNamespace(returncode=0))

    assert g.cmd_branches(branches_args(tmp_path, delete_fork=True)) == 0
    assert deleted == [] and "would delete fork done" in capsys.readouterr().out

    assert g.cmd_branches(branches_args(tmp_path, delete_fork=True, yes=True)) == 0
    out = capsys.readouterr().out
    assert deleted == [["gh", "api", "-X", "DELETE", "repos/me/repo/git/refs/heads/done"]]
    assert "skip fork moved (moved since check)" in out
    assert g.DELETE_LOG.read_text().split()[1:] == ["me/repo", "fork", "done", "a" * 40]


def test_fork_branches_read_across_pages(clone, monkeypatch):
    sha = sh(clone, "rev-parse", "main")
    sh(clone, "remote", "rename", "origin", "upstream")
    sh(clone, "remote", "add", "origin", "https://github.com/me/repo.git")
    sh(clone, "config", "--add", f"url.{clone.parent / 'remote.git'}.insteadOf", "https://github.com/me/repo.git")
    monkeypatch.setattr(g, "gh_slug", lambda u: "org/repo")  # upstream and fork both resolve to the bare repo
    fake_gh(monkeypatch, fork_branches=[("main", sha), ("feature/a", sha), ("x", "f" * 40)])
    rep = g.branch_report(clone, g.DEFAULT_KEEP)
    assert [(b["branch"], b["merged"], b["known"]) for b in rep["fork_branches"]] == [
        ("main", "merged", True), ("feature/a", "merged", True), ("x", None, False)]


def test_close_previews_skips_and_closes(monkeypatch, capsys):
    prs = {("o/r", 1): {"state": "OPEN", "author": {"login": "me"}, "title": "mine"},
           ("o/r", 2): {"state": "MERGED", "author": {"login": "me"}, "title": "merged"},
           ("o/r", 3): {"state": "OPEN", "author": {"login": "other"}, "title": "theirs"}}
    monkeypatch.setattr(g, "me", lambda: "me")
    monkeypatch.setattr(g, "gh_json", lambda *a: prs[(a[4], int(a[2]))])
    ran = []
    monkeypatch.setattr(g.subprocess, "run", lambda cmd, **k: ran.append(cmd) or SimpleNamespace(returncode=0, stderr=""))
    args = SimpleNamespace(prs=["o/r#1", "o/r#2", "https://github.com/o/r/pull/3"], message="why", yes=False,
                           any_author=False)
    assert g.cmd_close(args) == 0
    out = capsys.readouterr().out
    assert ran == [] and "would close o/r#1" in out and "skip   o/r#2 (merged)" in out and "author other" in out
    args.yes = True
    assert g.cmd_close(args) == 0
    assert ran == [["gh", "pr", "close", "1", "-R", "o/r", "-c", "why"]]


def test_close_401_stops(monkeypatch):
    monkeypatch.setattr(g, "me", lambda: "me")
    monkeypatch.setattr(g, "gh_json", lambda *a: {"state": "OPEN", "author": {"login": "me"}, "title": "t"})
    monkeypatch.setattr(g.subprocess, "run", lambda cmd, **k: SimpleNamespace(returncode=1, stderr="HTTP 401"))
    with pytest.raises(g.GhError):
        g.cmd_close(SimpleNamespace(prs=["o/r#1"], message="m", yes=True, any_author=False))


def test_remotes_fix_rewrites_fetch_and_push_urls(tmp_path, capsys):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sh(repo, "remote", "add", "origin", "https://u:ghp_x@github.com/o/r.git")
    sh(repo, "config", "remote.origin.pushurl", "https://u:ghp_x@github.com/o/r.git")
    sh(repo, "remote", "add", "clean", "git@github.com:o/c.git")
    args = SimpleNamespace(root=str(tmp_path), fix=False)
    assert g.cmd_remotes(args) == 0
    out = capsys.readouterr().out
    assert "ghp_x" not in out and "2 remote URL(s)" in out
    assert "ghp_x" in sh(repo, "remote", "get-url", "origin")
    args.fix = True
    g.cmd_remotes(args)
    assert sh(repo, "remote", "get-url", "origin") == "git@github.com:o/r.git"
    assert sh(repo, "config", "--get", "remote.origin.pushurl") == "git@github.com:o/r.git"
    assert sh(repo, "remote", "get-url", "clean") == "git@github.com:o/c.git"
