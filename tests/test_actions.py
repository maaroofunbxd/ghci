import importlib
import json


def _module():
    """A fresh copy of ghci.actions (tests monkeypatch its globals)."""
    return importlib.reload(importlib.import_module("ghci.actions"))


def _run(id_, wf, sha="aaa1111", status="completed", conclusion="success"):
    return {"databaseId": id_, "workflowName": wf, "name": "title", "headSha": sha, "headBranch": "main",
            "status": status, "conclusion": conclusion, "url": f"https://x/{id_}"}


def _fake_gh(monkeypatch, mod, listing, views=None, jobs=None, log="boom\nfailed here"):
    """gh stand-in: `run list` returns `listing` (callable or list), `run view ID` pops from views[ID]."""
    calls = []
    views = views or {}

    def gh_text(env, *args):
        calls.append(args)
        if args[:2] == ("run", "list"):
            return json.dumps(listing() if callable(listing) else listing)
        if args[:2] == ("run", "view") and "--log-failed" in args:
            return log
        if args[:2] == ("run", "view") and args[-1] == "jobs":
            return json.dumps({"jobs": (jobs or {}).get(int(args[2]), [])})
        if args[:2] == ("run", "view"):
            seq = views[int(args[2])]
            return json.dumps(seq.pop(0) if len(seq) > 1 else seq[0])
        raise AssertionError(args)

    monkeypatch.setattr(mod, "gh_text", gh_text)
    monkeypatch.setattr(mod, "cli_env", lambda: {})
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return calls


def test_runs_for_tip_takes_each_workflows_newest_run_of_the_newest_commit():
    mod = _module()
    runs = [_run(5, "tests"), _run(4, "lint"), _run(3, "tests"), _run(2, "tests", sha="old0000")]
    assert [r["databaseId"] for r in mod.runs_for_tip(runs)] == [5, 4]
    assert [r["databaseId"] for r in mod.runs_for_tip(runs, "old0000")] == [2]
    assert mod.runs_for_tip([]) == []


def test_status_reports_running_without_waiting(monkeypatch, capsys):
    mod = _module()
    _fake_gh(monkeypatch, mod, [_run(5, "tests", status="in_progress", conclusion=""), _run(4, "lint")])
    assert mod.main(["status", "-r", "o/r", "-b", "main"]) == mod.EXIT_RUNNING
    assert capsys.readouterr().out.splitlines()[-1] == "CI o/r@main aaa1111: running (1/2 runs ok)"


def test_watch_waits_then_shows_failed_steps_and_log_tail(monkeypatch, capsys):
    mod = _module()
    running = _run(5, "tests", status="in_progress", conclusion="")
    jobs = {5: [{"name": "pytest", "conclusion": "failure", "steps": [
        {"name": "setup", "conclusion": "success"}, {"name": "run", "conclusion": "failure"}]}]}
    _fake_gh(monkeypatch, mod, [running], views={5: [running, _run(5, "tests", conclusion="failure")]}, jobs=jobs)
    assert mod.main(["-r", "o/r", "-b", "main"]) == mod.EXIT_FAILED
    out = capsys.readouterr().out
    assert "❌ pytest: failure" in out and "❌ run: failure" in out and "setup" not in out
    assert "failed here" in out
    assert out.splitlines()[-1] == "CI o/r@main aaa1111: failure (0/1 runs ok)"


def test_commit_watch_waits_for_runs_to_appear(monkeypatch, capsys):
    mod = _module()
    listings = [[], [], [_run(7, "tests", sha="abc1234")]]
    calls = _fake_gh(monkeypatch, mod, lambda: listings.pop(0) if len(listings) > 1 else listings[0])
    assert mod.main(["-r", "o/r", "--commit", "abc1234"]) == mod.EXIT_OK
    lists = [c for c in calls if c[:2] == ("run", "list")]
    assert len(lists) == 3 and "--commit" in lists[0] and "--branch" not in lists[0]
    assert capsys.readouterr().out.splitlines()[-1].endswith("success (1/1 runs ok)")


def test_no_runs_is_an_error(monkeypatch):
    mod = _module()
    _fake_gh(monkeypatch, mod, [])
    assert mod.main(["status", "-r", "o/r", "-b", "nope"]) == mod.EXIT_ERROR


def test_bare_repo_name_takes_the_cwd_org(monkeypatch):
    mod = _module()
    monkeypatch.setenv("GHCI_OWNER", "envorg")
    monkeypatch.setattr(mod, "current_repo", lambda: "someorg/this")
    assert mod.full_repo("app") == "someorg/app"
    assert mod.full_repo("x/app") == "x/app"
    monkeypatch.setattr(mod, "current_repo", lambda: None)
    assert mod.full_repo("app") == "envorg/app"
    monkeypatch.delenv("GHCI_OWNER")
    assert mod.full_repo("app") == "app"


def _job(name, status="completed", conclusion="success", id_=900):
    return {"databaseId": id_, "name": name, "status": status, "conclusion": conclusion, "steps": []}


def test_commit_watch_with_job_returns_when_the_job_is_done_not_the_run(monkeypatch, capsys):
    mod = _module()
    running = _run(5, "app", status="in_progress", conclusion="")
    # The job finishes on the 2nd poll while the run stays in progress (the run view never completes).
    job_states = [[_job("build", "in_progress", ""), _job("other", "in_progress", "")],
                  [_job("build"), _job("other", "in_progress", "")]]
    calls = _fake_gh(monkeypatch, mod, [running], views={5: [running]})
    real = mod.gh_text

    def gh_text(env, *args):
        if args[:2] == ("run", "view") and args[-1] == "jobs":
            calls.append(args)
            return json.dumps({"jobs": job_states.pop(0) if len(job_states) > 1 else job_states[0]})
        return real(env, *args)

    monkeypatch.setattr(mod, "gh_text", gh_text)
    assert mod.main(["-r", "o/r", "--commit", "aaa1111", "-j", "build"]) == mod.EXIT_OK
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == "CI o/r@main aaa1111: job build success (run still in_progress)"
    assert "other" not in out


def test_job_is_waited_for_in_whichever_run_has_it_and_failure_shows_its_log(monkeypatch, capsys):
    mod = _module()
    runs = [_run(5, "lint", status="in_progress", conclusion=""), _run(6, "build", status="in_progress", conclusion="")]
    jobs = {5: [_job("ruff", "in_progress", "")],
            6: [{**_job("compile", conclusion="failure", id_=77),
                 "steps": [{"name": "make", "conclusion": "failure"}]}]}
    calls = _fake_gh(monkeypatch, mod, runs, views={5: [runs[0]], 6: [runs[1]]}, jobs=jobs)
    assert mod.main(["-r", "o/r", "-c", "aaa1111", "-j", "compile"]) == mod.EXIT_FAILED
    out = capsys.readouterr().out
    assert "❌ make: failure" in out and "failed here" in out
    assert out.splitlines()[-1] == "CI o/r@main aaa1111: job compile failure (run still in_progress)"
    log_call = next(c for c in calls if "--log-failed" in c)
    assert log_call[2] == "6" and ("--job", "77") == log_call[5:7]


def test_branch_watch_with_job_uses_the_same_path(monkeypatch, capsys):
    mod = _module()
    running = _run(5, "tests", status="in_progress", conclusion="")
    _fake_gh(monkeypatch, mod, [running], views={5: [running]}, jobs={5: [_job("pytest", conclusion="skipped")]})
    assert mod.main(["-r", "o/r", "-b", "main", "-j", "pytest"]) == mod.EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1] == "CI o/r@main aaa1111: job pytest skipped (run still in_progress)"


def test_run_watch_with_job(monkeypatch, capsys):
    mod = _module()
    done = _run(9, "tests", conclusion="failure")
    _fake_gh(monkeypatch, mod, [], views={9: [done]}, jobs={9: [_job("pytest")]})
    assert mod.main(["-r", "o/r", "--run", "9", "-j", "pytest"]) == mod.EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1] == "CI o/r@main aaa1111: job pytest success (run failure)"


def test_job_missing_waits_for_the_runs_then_errors(monkeypatch, capsys):
    mod = _module()
    running = _run(5, "tests", status="in_progress", conclusion="")
    calls = _fake_gh(monkeypatch, mod, [running], views={5: [running, running, _run(5, "tests")]},
                     jobs={5: [_job("pytest")]})
    assert mod.main(["-r", "o/r", "-c", "aaa1111", "-j", "nope"]) == mod.EXIT_ERROR
    captured = capsys.readouterr()
    assert sum(1 for c in calls if c[:2] == ("run", "view") and c[-1] != "jobs") == 3  # waited for the run to end
    assert captured.out.splitlines()[-1] == "CI o/r@main aaa1111: job nope not found (not in tests)"
    assert "no job named 'nope'" in captured.err


def test_job_watch_timeout(monkeypatch, capsys):
    mod = _module()
    running = _run(5, "tests", status="in_progress", conclusion="")
    _fake_gh(monkeypatch, mod, [running], views={5: [running]}, jobs={5: [_job("pytest", "in_progress", "")]})
    clock = iter(range(0, 10_000, 50))
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
    assert mod.main(["-r", "o/r", "-c", "aaa1111", "-j", "pytest", "--timeout", "120"]) == mod.EXIT_TIMEOUT
    assert capsys.readouterr().out.splitlines()[-1] == "CI o/r@main aaa1111: job pytest timeout (run still in_progress)"


def test_status_with_job(monkeypatch, capsys):
    mod = _module()
    running = _run(5, "tests", status="in_progress", conclusion="")
    jobs = {5: [_job("done"), _job("busy", "in_progress", "")]}
    _fake_gh(monkeypatch, mod, [running], jobs=jobs)
    assert mod.main(["status", "-r", "o/r", "-b", "main", "-j", "done"]) == mod.EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1] == "CI o/r@main aaa1111: job done success (run still in_progress)"
    assert mod.main(["status", "-r", "o/r", "-b", "main", "-j", "busy"]) == mod.EXIT_RUNNING
    assert capsys.readouterr().out.splitlines()[-1] == "CI o/r@main aaa1111: job busy running (run still in_progress)"
    assert mod.main(["status", "-r", "o/r", "-b", "main", "-j", "later"]) == mod.EXIT_RUNNING
    assert capsys.readouterr().out.splitlines()[-1] == "CI o/r@main aaa1111: job later not started (not yet in tests)"


def test_token_comes_from_env_or_gh_login_never_a_file(monkeypatch, tmp_path):
    mod = _module()
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".env").write_text("SOME_SECRET_KEY=leaked\nGITHUB_TOKEN=filetok\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path / "proj")
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SOME_SECRET_KEY", "leaked")
    assert mod.get_github_token() is None
    monkeypatch.setenv("GITHUB_TOKEN", "gt")
    assert mod.get_github_token() == "gt"
    monkeypatch.setenv("GH_TOKEN", "ght")
    assert mod.get_github_token() == "ght"
    assert mod.cli_env()["GH_TOKEN"] == "ght"


def test_cli_env_uses_gh_login_without_injecting_a_token(monkeypatch):
    mod = _module()
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    env = mod.cli_env()
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env


# --- PRs from a fork --------------------------------------------------------------------------------

PR = {"number": 287, "url": "https://github.com/up/des/pull/287", "headRefName": "feat", "headRefOid": "edc6496" + "0" * 33,
      "headRepositoryOwner": {"login": "me"}, "baseRefName": "main"}
NOT_FOUND = "HTTP 404: Not Found (https://api.github.com/repos/x)"


def _cwd_clone(monkeypatch, mod, origin="me/des", upstream="up/des", branch="feat", head="edc6496" + "0" * 33):
    remotes = {"origin": f"git@github.com:{origin}.git", "upstream": f"git@github.com:{upstream}.git" if upstream else None}

    def git_out(*args):
        if args[:2] == ("remote", "get-url"):
            return remotes.get(args[2])
        return {("branch", "--show-current"): branch, ("rev-parse", "HEAD"): head}.get(args)
    monkeypatch.setattr(mod, "git_out", git_out)


def _gh_router(monkeypatch, mod, routes, checks=None):
    """gh_text stand-in: routes maps (repo, first two args) -> json-able value, or an exception to raise."""
    calls = []

    def gh_text(env, *args):
        calls.append(args)
        repo = args[args.index("--repo") + 1] if "--repo" in args else args[1]
        val = routes.get((repo, args[0], args[1]))
        if val is None:
            raise AssertionError(args)
        if isinstance(val, Exception):
            raise val
        return val if isinstance(val, str) else json.dumps(val() if callable(val) else val)

    monkeypatch.setattr(mod, "gh_text", gh_text)
    monkeypatch.setattr(mod, "cli_env", lambda: {})
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    if checks is not None:
        def run(cmd, **kw):
            calls.append(tuple(cmd[1:]))
            item = checks.pop(0) if len(checks) > 1 else checks[0]
            rc, out, err = item
            return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()
        monkeypatch.setattr(mod.subprocess, "run", run)
    return calls


def test_head_in_a_fork_clone_follows_the_pr_on_upstream(monkeypatch, capsys):
    mod = _module()
    _cwd_clone(monkeypatch, mod)
    run = {**_run(9, "des", sha=PR["headRefOid"]), "headBranch": "feat"}
    calls = _gh_router(monkeypatch, mod, {("up/des", "pr", "list"): [PR], ("up/des", "run", "list"): [run]})
    assert mod.main(["--head"]) == mod.EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1] == "CI up/des@feat edc6496: success (1/1 runs ok)"
    assert not any("me/des" in c for c in calls)
    listing = next(c for c in calls if c[:2] == ("run", "list"))
    assert "--commit" in listing and PR["headRefOid"] in listing


def test_fork_clone_without_pr_stays_on_origin(monkeypatch, capsys):
    mod = _module()
    _cwd_clone(monkeypatch, mod)
    _gh_router(monkeypatch, mod, {("up/des", "pr", "list"): [{**PR, "headRepositoryOwner": {"login": "someone"}}],
                                  ("me/des", "run", "list"): [_run(3, "ci")]})
    assert mod.main(["status"]) == mod.EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1].startswith("CI me/des@feat ")


def test_fork_without_upstream_remote_404_finds_parent_pr(monkeypatch, capsys):
    mod = _module()
    _cwd_clone(monkeypatch, mod, upstream=None)
    run = _run(9, "des", sha=PR["headRefOid"])
    _gh_router(monkeypatch, mod, {("me/des", "run", "list"): mod.NotFound(NOT_FOUND),
                                  ("repos/me/des", "api", "repos/me/des"): "up/des\n",
                                  ("up/des", "pr", "list"): [PR], ("up/des", "run", "list"): [run]})
    assert mod.main(["--head"]) == mod.EXIT_OK
    out = capsys.readouterr()
    assert out.out.splitlines()[-1] == "CI up/des@main edc6496: success (1/1 runs ok)"
    assert "up/des#287" in out.err


def test_run_404_mid_watch_follows_pr_checks_instead_of_waiting(monkeypatch, capsys):
    mod = _module()
    running = _run(38072195076, "des", status="in_progress", conclusion="", sha=PR["headRefOid"])
    pending = json.dumps([{"name": "build", "state": "IN_PROGRESS", "bucket": "pending", "workflow": "des",
                           "link": "https://github.com/up/des/actions/runs/1/job/2"}])
    done = json.dumps([{"name": "build", "state": "SUCCESS", "bucket": "pass", "workflow": "des",
                        "link": "https://github.com/up/des/actions/runs/1/job/2"}])
    calls = _gh_router(monkeypatch, mod, {("up/des", "run", "list"): [running],
                                          ("up/des", "run", "view"): mod.NotFound(NOT_FOUND),
                                          ("up/des", "pr", "list"): [PR]},
                       checks=[(1, "", "HTTP 401: Bad credentials"), (8, pending, ""), (0, done, "")])
    assert mod.main(["-r", "up/des", "-b", "feat"]) == mod.EXIT_OK
    out = capsys.readouterr()
    assert out.out.splitlines()[-1] == "CI up/des@feat edc6496: success (1/1 runs ok)"
    assert "following up/des#287's checks" in out.err
    assert sum(1 for c in calls if c[:2] == ("pr", "checks")) == 3  # the 401 was retried


def test_pr_checks_failure_exit_code_and_job(monkeypatch, capsys):
    mod = _module()
    checks = json.dumps([
        {"name": "build", "bucket": "fail", "workflow": "des", "link": "https://github.com/up/des/actions/runs/5/job/6"},
        {"name": "lint", "bucket": "pass", "workflow": "des", "link": ""}])
    _gh_router(monkeypatch, mod, {("up/des", "run", "list"): mod.NotFound(NOT_FOUND), ("up/des", "pr", "list"): [PR],
                                  ("up/des", "run", "view"): "x\tstep\tboom"}, checks=[(1, checks, "")])
    assert mod.main(["status", "-r", "up/des", "-b", "feat"]) == mod.EXIT_FAILED
    out = capsys.readouterr().out
    assert "❌ des / build: failure" in out and "boom" in out
    assert out.splitlines()[-1] == "CI up/des@feat edc6496: failure (1/2 runs ok)"
    assert mod.main(["status", "-r", "up/des", "-b", "feat", "-j", "lint", "--log-lines", "0"]) == mod.EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1] == "CI up/des@feat edc6496: job lint success (run success)"


def test_run_404_without_pr_fails_fast(monkeypatch, capsys):
    mod = _module()
    _gh_router(monkeypatch, mod, {("o/r", "run", "view"): mod.NotFound(NOT_FOUND), ("o/r", "pr", "list"): []})
    assert mod.main(["--run", "123", "-r", "o/r"]) == mod.EXIT_ERROR
    assert "GitHub returned 404 for run 123" in capsys.readouterr().err


def test_404_mid_watch_without_pr_fails_fast(monkeypatch, capsys):
    mod = _module()
    running = _run(5, "tests", status="in_progress", conclusion="")
    _gh_router(monkeypatch, mod, {("o/r", "run", "list"): [running], ("o/r", "run", "view"): mod.NotFound(NOT_FOUND),
                                  ("o/r", "pr", "list"): []})
    assert mod.main(["-r", "o/r", "-b", "main"]) == mod.EXIT_ERROR
    assert "404" in capsys.readouterr().err


def test_gh_text_raises_not_found_on_404(monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: type("R", (), {
        "returncode": 1, "stdout": "", "stderr": NOT_FOUND})())
    import pytest
    with pytest.raises(mod.NotFound):
        mod.gh_text({}, "run", "view", "1")


def test_pr_head_behind_the_commit_is_an_error_not_a_wrong_answer(monkeypatch, capsys):
    mod = _module()
    _gh_router(monkeypatch, mod, {("up/des", "run", "list"): mod.NotFound(NOT_FOUND), ("up/des", "pr", "list"): [PR]},
               checks=[(0, "[]", "")])
    assert mod.main(["status", "-r", "up/des", "-b", "feat", "-c", "1234567"]) == mod.EXIT_ERROR
    assert "head is edc6496, not 1234567" in capsys.readouterr().err
