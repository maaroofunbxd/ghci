#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""ghci: check or wait for GitHub Actions CI on any repo, with a short result (and the failed log) at the end.

Part of the ghci package (with ghclean, ghci/clean.py). Keep it one file, stdlib only (argcomplete optional),
importing nothing else from the package, so it can be copied anywhere and run as a script. run_from_task is
for a job runner that watches a run or job from a task description: keep its signature.

    ghci                          wait for CI on the current repo's branch tip; exit 0 on success
    ghci status                   current state only, no waiting (exit 3 = still running)
    ghci --head                   CI for local HEAD (waits for the runs to appear after a push)
    ghci -r octo/app -b gliner -j "Build and push"
    ghci --run 123456789 --steps --log-file ~/ci.log
    ghci -r octo/app -c 1a2b3c4 -j build_images   returns once that job is done

What is checked: every workflow's latest run for one commit (--run: that run only; else --commit/--head:
that commit; else the branch's newest run's commit). The last line is one summary line:
    CI octo/app@gliner 1a2b3c4: success (2/2 runs ok)
and on failure the failed jobs/steps plus the end of `gh run view --log-failed` come before it. With
--job, only that job counts (watch stops when it finishes, even if its run goes on):
    CI octo/app@gliner 1a2b3c4: job build success (run still in_progress)

A branch with an open PR from a fork: in a fork clone (an `upstream` remote; or origin is a fork and has no runs
of its own) ghci follows the PR's CI on the base repo, for the PR's head commit. When the Actions API answers 404 for the runs (a token
that cannot read them), it follows the PR's checks (`gh pr checks`, each check counting as one run in the
summary line) and exits 2 straight away when there is no PR.

Exit codes: 0 success, 1 failed or cancelled, 2 error (bad args, gh error, no runs), 3 still running
(status only), 124 --timeout reached.

Token: GH_TOKEN / GITHUB_TOKEN from the environment, else gh's own login (`gh auth login`). run_from_task
(the runner) skips gh's login: the runner's job sets GH_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_FAILED, EXIT_ERROR, EXIT_RUNNING, EXIT_TIMEOUT = 0, 1, 2, 3, 124
RUN_FIELDS = "databaseId,status,conclusion,name,workflowName,headSha,headBranch,createdAt,url,event"
OK_CONCLUSIONS = ("success", "skipped", "neutral")


def get_github_token() -> str | None:
    """GH_TOKEN / GITHUB_TOKEN from the environment (the runner sets both to its org PAT), else None."""
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = (os.environ.get(key) or "").strip()
        if token:
            return token
    return None


def gh_env(token: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    env["NO_COLOR"] = "1"
    env["CLICOLOR"] = "0"
    return env


def cli_env() -> dict[str, str]:
    """For ghci: a token from the environment, else gh's own login."""
    if get_github_token():
        return gh_env(get_github_token())
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    for _ in range(2):  # the keychain read fails now and then
        if subprocess.run(["gh", "auth", "token"], capture_output=True, env=env, check=False).returncode == 0:
            return gh_env(None)
    print("gh is not logged in (or its keychain read failed): run `gh auth login`, or set GH_TOKEN", file=sys.stderr)
    return gh_env(None)


def clean_gh_output(output: str) -> str:
    text = output.strip()
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return text.strip("﻿").lstrip("​﻿")


def parse_gh_json(output: str) -> Any:
    text = clean_gh_output(output)
    if not text or text == "null":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


class NotFound(RuntimeError):
    """gh got an HTTP 404: the run or repo is not visible with this token (or does not exist)."""


def gh_text(env: dict[str, str], *args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if "HTTP 404" in err:
            raise NotFound(err)
        if "401" in err or "Bad credentials" in err:
            raise RuntimeError(
                f"{err}\n"
                "Hint: use a PAT with actions:read on the target repo "
                "(set GH_TOKEN; the workflow GITHUB_TOKEN only covers this repo), "
                "or check `gh auth status`."
            )
        raise RuntimeError(err or f"gh {' '.join(args)} failed")
    return result.stdout


def gh_json(env: dict[str, str], *args: str) -> Any:
    return parse_gh_json(gh_text(env, *args))


def git_out(*args: str) -> str | None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def remote_repo(remote: str) -> str | None:
    """owner/name of one of the cwd clone's GitHub remotes."""
    url = git_out("remote", "get-url", remote)
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return match.group(1) if match else None


def current_repo() -> str | None:
    """owner/name of the cwd's origin remote."""
    return remote_repo("origin")


def full_repo(repo: str) -> str:
    """`app` -> `octo/app`: a bare name is taken to be in the cwd repo's owner, else $GHCI_OWNER; with neither it
    is left bare (gh then says it wants OWNER/REPO)."""
    if "/" in repo:
        return repo
    owner = default_owner()
    return f"{owner}/{repo}" if owner else repo


def default_owner() -> str | None:
    """The cwd clone's origin owner, else $GHCI_OWNER."""
    return (current_repo() or "").split("/")[0] or os.environ.get("GHCI_OWNER") or None


# --- the runner's job/run watch (run_from_task) -----------------------------------------------------


def fetch_latest_run(
    repo: str,
    branch: str,
    env: dict[str, str],
    user: str | None = None,
) -> dict[str, Any]:
    args = [
        "run",
        "list",
        "--repo",
        repo,
        "--branch",
        branch,
        "--limit",
        "1",
        "--json",
        "databaseId,status,conclusion,name,createdAt",
        "--jq",
        ".[0]",
    ]
    if user:
        args.extend(["--user", user])
    run_info = gh_json(env, *args)
    if not isinstance(run_info, dict) or "databaseId" not in run_info:
        raise RuntimeError(f"No runs found for {repo} branch {branch}")
    return run_info


def fetch_run_jobs(run_id: str, repo: str, env: dict[str, str]) -> list[dict[str, Any]]:
    data = gh_json(env, "run", "view", str(run_id), "--repo", repo, "--json", "jobs") or {}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return jobs if isinstance(jobs, list) else []


def find_job(jobs: list[dict[str, Any]], job_name: str) -> dict[str, Any] | None:
    for job in jobs:
        if job.get("name") == job_name:
            return job
    return None


def format_job(job: dict[str, Any], prefix: str = "") -> str:
    name = prefix + job.get("name", "?")
    conclusion = job.get("conclusion") or ""
    status = job.get("status") or ""
    if conclusion == "success":
        return f"✅ {name}: success"
    if conclusion == "skipped":
        return f"⏭️  {name}: skipped"
    if conclusion == "failure":
        return f"❌ {name}: failure"
    if conclusion == "cancelled":
        return f"🛑 {name}: cancelled"
    return f"⏳ {name}: {status or conclusion or 'pending'}"


def watch_job(
    *,
    repo: str,
    branch: str,
    job_name: str,
    user: str | None,
    env: dict[str, str],
    poll_seconds: int = 30,
    require_success: bool = False,
) -> dict[str, Any]:
    run_info = fetch_latest_run(repo, branch, env, user=user)
    run_id = str(run_info["databaseId"])
    print("🔍 GitHub Actions job watch")
    print(f"   Repo:   {repo}")
    print(f"   Branch: {branch}")
    if user:
        print(f"   User:   {user}")
    print(f"   Run ID: {run_id}")
    print(f"   Job:    {job_name}")
    print()

    last_line = ""
    while True:
        jobs = fetch_run_jobs(run_id, repo, env)
        job = find_job(jobs, job_name)
        if job:
            line = format_job(job)
            if line != last_line:
                print(line)
                last_line = line
            if job.get("status") == "completed":
                conclusion = job.get("conclusion")
                print()
                if require_success and conclusion != "success":
                    raise RuntimeError(f"Job '{job_name}' finished with conclusion={conclusion}")
                print(f"Job '{job_name}' completed (conclusion={conclusion})")
                return job
        else:
            print(f"⏳ Waiting for job '{job_name}' to appear in run {run_id}...")
        time.sleep(poll_seconds)


def watch_run(
    *,
    repo: str,
    branch: str,
    user: str | None,
    env: dict[str, str],
    poll_seconds: int = 30,
    require_success: bool = True,
) -> dict[str, Any]:
    run_info = fetch_latest_run(repo, branch, env, user=user)
    run_id = str(run_info["databaseId"])
    print(f"🔍 Watching run {run_id} on {repo} ({branch})")
    while True:
        run_info = gh_json(
            env,
            "run",
            "view",
            run_id,
            "--repo",
            repo,
            "--json",
            "databaseId,status,conclusion,name,createdAt",
        )
        if not isinstance(run_info, dict):
            raise RuntimeError(f"Could not read run {run_id}")
        if run_info.get("status") == "completed":
            conclusion = run_info.get("conclusion")
            if require_success and conclusion != "success":
                raise RuntimeError(f"Run {run_id} finished with conclusion={conclusion}")
            return run_info
        time.sleep(poll_seconds)


def run_from_task(task: dict[str, Any]) -> None:
    env = gh_env(get_github_token())
    repo = task["repo"]
    branch = task["branch"]
    user = task.get("user")
    job = task.get("job")
    poll_seconds = int(task.get("poll_seconds", 30))
    require_success = bool(task.get("require_success", False))

    if job:
        watch_job(
            repo=repo,
            branch=branch,
            job_name=job,
            user=user,
            env=env,
            poll_seconds=poll_seconds,
            require_success=require_success,
        )
        return

    watch_run(
        repo=repo,
        branch=branch,
        user=user,
        env=env,
        poll_seconds=poll_seconds,
        require_success=require_success,
    )


# --- ghci: every workflow's run for one commit ------------------------------------------------------


def list_runs(
    env: dict[str, str],
    repo: str,
    *,
    branch: str | None = None,
    commit: str | None = None,
    workflow: str | None = None,
    user: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    args = ["run", "list", "--repo", repo, "--limit", str(limit), "--json", RUN_FIELDS]
    for flag, value in (("--branch", branch), ("--commit", commit), ("--workflow", workflow), ("--user", user)):
        if value:
            args.extend([flag, value])
    runs = gh_json(env, *args)
    return runs if isinstance(runs, list) else []


def runs_for_tip(runs: list[dict[str, Any]], sha: str | None = None) -> list[dict[str, Any]]:
    """Each workflow's newest run for `sha` (default: the newest run's commit). gh lists newest first."""
    if not runs:
        return []
    sha = sha or runs[0].get("headSha")
    picked: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.get("headSha") != sha:
            continue
        picked.setdefault(run_label(run), run)
    return list(picked.values())


def select_runs(env: dict[str, str], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.run:
        run = gh_json(env, "run", "view", str(args.run), "--repo", args.repo, "--json", RUN_FIELDS)
        return [run] if isinstance(run, dict) else []
    runs = list_runs(
        env, args.repo, branch=None if args.commit else args.branch, commit=args.commit,
        workflow=args.workflow, user=args.user,
    )
    return runs_for_tip(runs, args.commit)


def view_run(env: dict[str, str], repo: str, run_id: Any) -> dict[str, Any]:
    run = gh_json(env, "run", "view", str(run_id), "--repo", repo, "--json", RUN_FIELDS)
    if not isinstance(run, dict):
        raise RuntimeError(f"Could not read run {run_id}")
    return run


def run_label(run: dict[str, Any]) -> str:
    return run.get("workflowName") or run.get("name") or str(run.get("databaseId"))


def is_done(runs: list[dict[str, Any]]) -> bool:
    return bool(runs) and all(run.get("status") == "completed" for run in runs)


def is_ok(runs: list[dict[str, Any]]) -> bool:
    return is_done(runs) and all(run.get("conclusion") in OK_CONCLUSIONS for run in runs)


def summary_line(repo: str, branch: str | None, runs: list[dict[str, Any]], state: str | None = None) -> str:
    sha = (runs[0].get("headSha") or "")[:7] if runs else ""
    where = f"{repo}@{branch or (runs[0].get('headBranch') if runs else '')}"
    ok = sum(1 for run in runs if run.get("conclusion") in OK_CONCLUSIONS)
    if state is None:
        state = ("success" if is_ok(runs) else "failure") if is_done(runs) else "running"
    return f"CI {where} {sha}: {state} ({ok}/{len(runs)} runs ok)"


def print_runs(runs: list[dict[str, Any]], env: dict[str, str], repo: str, job: str | None, steps: bool) -> None:
    """One line per run; its jobs (and failed steps) when it failed or with --job/--steps."""
    for run in runs:
        print(f"{format_job({**run, 'name': run_label(run)})}  {run.get('url', '')}")
        failed = run.get("status") == "completed" and run.get("conclusion") not in OK_CONCLUSIONS
        if not (job or steps or failed):
            continue
        for j in fetch_run_jobs(run["databaseId"], repo, env):
            if job and j.get("name") != job:
                continue
            print(f"   {format_job(j)}")
            for step in j.get("steps") or []:
                if steps or step.get("conclusion") in ("failure", "cancelled"):
                    print(f"      {format_job(step)}")


def failed_log_tail(env: dict[str, str], repo: str, run_id: Any, lines: int, job_id: Any = None) -> str:
    job_args = ("--job", str(job_id)) if job_id else ()  # one job's log is there before the run ends
    try:
        text = gh_text(env, "run", "view", str(run_id), "--repo", repo, *job_args, "--log-failed")
    except RuntimeError as exc:
        return f"(could not fetch failed log: {exc})"
    tail = clean_gh_output(text).splitlines()[-lines:]
    # "job<TAB>step<TAB>2026-10-08T21:06:24.84Z msg" -> "job<TAB>step<TAB>msg"
    return "\n".join(re.sub(r"^([^\t]*\t[^\t]*\t)\d{4}-\d\d-\d\dT[\d:.]+Z ?", r"\1", line) for line in tail)


def append_log_line(log_file: str, line: str) -> None:
    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {line}\n")


def log_new_successes(
    jobs: list[dict[str, Any]], logged: set[tuple[str, ...]], log_file: str | None, steps: bool, prefix: str
) -> None:
    """Print (and append to --log-file) each job, and with --steps each step, once it succeeds."""
    for j in jobs:
        name = j.get("name", "?")
        items = [(("job", prefix, name), j, "")]
        if steps:
            items += [(("step", prefix, name, s.get("name", "?")), s, f"{name} :: ") for s in j.get("steps") or []]
        for key, item, label in items:
            if key in logged or item.get("conclusion") != "success":
                continue
            line = format_job(item, prefix + label)
            print(line)
            if log_file:
                append_log_line(log_file, line)
            logged.add(key)


# --- PRs from a fork: follow the PR on the base repo ------------------------------------------------

PR_FIELDS = "number,url,headRefName,headRefOid,headRepositoryOwner,baseRefName"
CHECK_CONCLUSIONS = {"pass": "success", "fail": "failure", "skipping": "skipped", "cancel": "cancelled"}


def find_open_pr(
    env: dict[str, str], repos: list[str | None], branch: str, head_owner: str | None = None
) -> dict[str, Any] | None:
    """The first open PR with head `branch` (from `head_owner`, if given) on any of `repos`, plus its "repo"."""
    for repo in dict.fromkeys(r for r in repos if r):
        try:
            prs = gh_json(env, "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
                          "--json", PR_FIELDS) or []
        except NotFound:
            continue
        for pr in prs:
            owner = ((pr.get("headRepositoryOwner") or {}).get("login") or "").lower()
            if head_owner is None or owner == head_owner.lower():
                return {**pr, "repo": repo}
    return None


def parent_repo(env: dict[str, str], repo: str) -> str | None:
    """The repo `repo` was forked from, if it is a fork."""
    try:
        return gh_text(env, "api", f"repos/{repo}", "--jq", ".parent.full_name // empty").strip() or None
    except NotFound:
        return None


def lookup_pr(env: dict[str, str], args: argparse.Namespace) -> dict[str, Any] | None:
    """The open PR for the branch: on the cwd clone's upstream remote or origin's parent (head owner = origin's
    owner), else on --repo itself."""
    branch = args.branch or args.pr_branch
    if not branch or args.run:
        return None
    if args.in_cwd_repo:
        base = args.cwd_upstream if args.cwd_upstream and args.cwd_upstream != args.repo else parent_repo(env, args.repo)
        if base:
            pr = find_open_pr(env, [base], branch, head_owner=args.repo.split("/")[0])
            if pr:
                return pr
    return find_open_pr(env, [args.repo], branch)


def follow_pr(args: argparse.Namespace, pr: dict[str, Any], why: str = "") -> None:
    """Point args at the PR's base repo and head commit."""
    args.pr = pr
    if pr["repo"] != args.repo:
        print(f"{args.branch or args.pr_branch} is {pr['repo']}#{pr['number']}{why}: following its CI there",
              file=sys.stderr)
    args.repo = pr["repo"]
    if not args.commit:
        args.commit = pr.get("headRefOid")


def pr_checks(env: dict[str, str], repo: str, number: Any) -> list[dict[str, Any]]:
    """`gh pr checks` (exit 8 = pending, 1 = a check failed, both with JSON); a 401 is retried once."""
    args = ["gh", "pr", "checks", str(number), "--repo", repo, "--json", "name,state,bucket,link,workflow"]
    for attempt in range(2):
        result = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
        err = (result.stderr or "").strip()
        if "401" in err and attempt == 0:
            time.sleep(3)
            continue
        if result.returncode in (0, 1, 8) and clean_gh_output(result.stdout):
            checks = parse_gh_json(result.stdout)
            return checks if isinstance(checks, list) else []
        if "no checks reported" in err:
            return []
        if "HTTP 404" in err:
            raise NotFound(err)
        raise RuntimeError(err or f"gh pr checks {number} --repo {repo} failed")
    return []


def check_as_run(check: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    """A PR check in the shape of a run (each check counts as one run in the summary line)."""
    conclusion = CHECK_CONCLUSIONS.get(check.get("bucket") or "", "")
    workflow = check.get("workflow")
    run_id = re.search(r"/actions/runs/(\d+)/job/(\d+)", check.get("link") or "")
    return {"databaseId": run_id.group(1) if run_id else None, "jobId": run_id.group(2) if run_id else None,
            "workflowName": f"{workflow} / {check.get('name')}" if workflow else check.get("name"),
            "name": check.get("name"), "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "url": check.get("link") or "",
            "headSha": pr.get("headRefOid") or "", "headBranch": pr.get("headRefName") or ""}


def pr_runs(env: dict[str, str], args: argparse.Namespace) -> list[dict[str, Any]]:
    runs = [check_as_run(c, args.pr) for c in pr_checks(env, args.repo, args.pr["number"])]
    if args.job:
        runs = [r for r in runs if r["name"] == args.job]
    if args.workflow:
        runs = [r for r in runs if r["workflowName"].split(" / ")[0] == args.workflow]
    return runs


def ghci_pr(env: dict[str, str], args: argparse.Namespace, started: float) -> int:
    """Follow an open PR's checks (`gh pr checks`), for when the Actions runs API gives 404s."""
    pr, repo = args.pr, args.repo
    if args.commit and not (pr.get("headRefOid") or "").startswith(args.commit):
        print(f"ERROR: {repo}#{pr['number']} head is {(pr.get('headRefOid') or '?')[:7]}, not {args.commit[:7]}: "
              "its checks are for another commit (push not reached the PR yet? rerun, or drop --head/--commit)",
              file=sys.stderr)
        return EXIT_ERROR
    branch = args.branch or pr.get("headRefName")
    if args.mode == "watch":
        print(f"🔍 Following the checks of {repo}#{pr['number']}: {pr.get('url', '')}")
    while True:
        runs = pr_runs(env, args)
        if runs and (is_done(runs) or args.mode == "status"):
            break
        waited = time.monotonic() - started
        if not runs and (args.mode == "status" or waited > args.appear_seconds):
            what = f"no check named {args.job!r}" if args.job else "no checks"
            print(f"ERROR: {what} on {repo}#{pr['number']}", file=sys.stderr)
            return EXIT_ERROR
        if args.timeout and waited > args.timeout:
            print(summary_line(repo, branch, runs, "timeout"))
            return EXIT_TIMEOUT
        time.sleep(args.poll_seconds)

    def summary(state: str | None = None) -> str:
        if args.job:  # the one check named --job
            state = state or (runs[0]["conclusion"] if is_done(runs) else "running")
            return job_summary_line(repo, branch, runs, args.job, state, runs[0])
        return summary_line(repo, branch, runs, state)

    for run in runs:
        print(f"{format_job({**run, 'name': run_label(run)})}  {run['url']}")
    if not is_done(runs):
        print(summary())
        return EXIT_RUNNING
    if not is_ok(runs) and args.log_lines:
        for run in runs:
            if run.get("conclusion") not in OK_CONCLUSIONS and run["databaseId"]:
                print(f"\n--- {run_label(run)}: last {args.log_lines} lines of the failed steps ---")
                print(failed_log_tail(env, repo, run["databaseId"], args.log_lines, run["jobId"]))
    print(summary())
    return EXIT_OK if is_ok(runs) else EXIT_FAILED


def not_found_message(args: argparse.Namespace, exc: Exception) -> str:
    what = f"run {args.run}" if args.run else f"the Actions runs of {args.repo}"
    return (f"GitHub returned 404 for {what}: {str(exc).splitlines()[0] if str(exc) else ''}\n"
            "The repo or run is not visible to this token (a private repo it cannot read, a fork with Actions "
            "off, a deleted run). For a PR from a fork pass the base repo: ghci -r OWNER/REPO -b BRANCH.")


# --- ghci -------------------------------------------------------------------------------------------


def ghci(args: argparse.Namespace) -> int:
    """Runs of the target; a branch with an open PR from a fork follows the PR on its base repo, and when the
    Actions API answers 404 the PR's checks are followed instead (no PR: fail fast)."""
    env = cli_env()
    started = time.monotonic()
    args.pr = None
    branch = args.branch or args.pr_branch
    # A fork clone (upstream remote): its PR's CI runs on the base repo, not on origin.
    if args.in_cwd_repo and args.cwd_upstream and args.cwd_upstream != args.repo and branch and not args.run:
        pr = find_open_pr(env, [args.cwd_upstream], branch, head_owner=args.repo.split("/")[0])
        if pr:
            follow_pr(args, pr)
    try:
        return ghci_runs(env, args, started)
    except NotFound as exc:
        if args.pr is None:
            pr = lookup_pr(env, args)
            if pr:
                moved = pr["repo"] != args.repo
                follow_pr(args, pr, f" (the Actions API answered 404 on {args.repo})")
                if moved:  # a fork's runs 404: try the base repo's runs first
                    try:
                        return ghci_runs(env, args, started)
                    except NotFound:
                        pass
        if args.pr is None:
            print(f"ERROR: {not_found_message(args, exc)}", file=sys.stderr)
            return EXIT_ERROR
        print(f"Actions API 404 on {args.repo}; following {args.repo}#{args.pr['number']}'s checks instead",
              file=sys.stderr)
        return ghci_pr(env, args, started)


def ghci_runs(env: dict[str, str], args: argparse.Namespace, started: float) -> int:
    runs = select_runs(env, args)
    # Right after a push the commit's runs take a few seconds to show up.
    while not runs and args.commit and args.mode == "watch" and time.monotonic() - started < args.appear_seconds:
        time.sleep(min(args.poll_seconds, 10))
        runs = select_runs(env, args)
    if not runs and args.in_cwd_repo and args.pr is None and not args.run:
        pr = lookup_pr(env, args)
        if pr and pr["repo"] != args.repo:
            follow_pr(args, pr)
            return ghci_runs(env, args, started)
    if not runs:
        target = f"commit {args.commit[:7]}" if args.commit else f"branch {args.branch}"
        print(f"No runs found for {args.repo} {target}", file=sys.stderr)
        return EXIT_ERROR

    if args.job:
        return ghci_job(env, args, runs, started)

    if args.mode == "watch":
        print(f"🔍 Waiting on {len(runs)} run(s): {', '.join(run_label(r) for r in runs)}")
        logged: set[tuple[str, ...]] = set()
        while True:
            if args.log_file or args.steps:
                for run in runs:
                    jobs = fetch_run_jobs(run["databaseId"], args.repo, env)
                    log_new_successes(jobs, logged, args.log_file, args.steps, f"{run_label(run)} / ")
            if is_done(runs):
                break
            if args.timeout and time.monotonic() - started > args.timeout:
                print(summary_line(args.repo, args.branch, runs, "timeout"))
                return EXIT_TIMEOUT
            time.sleep(args.poll_seconds)
            runs = [view_run(env, args.repo, r["databaseId"]) for r in runs]

    print_runs(runs, env, args.repo, args.job, args.steps)
    if not is_done(runs):
        print(summary_line(args.repo, args.branch, runs))
        return EXIT_RUNNING
    if not is_ok(runs) and args.log_lines:
        for run in runs:
            if run.get("conclusion") not in OK_CONCLUSIONS:
                print(f"\n--- {run_label(run)}: last {args.log_lines} lines of the failed steps ---")
                print(failed_log_tail(env, args.repo, run["databaseId"], args.log_lines))
    print(summary_line(args.repo, args.branch, runs))
    return EXIT_OK if is_ok(runs) else EXIT_FAILED


def locate_job(
    env: dict[str, str], repo: str, runs: list[dict[str, Any]], job_name: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(run, job) for the first of `runs` that has a job named `job_name`, else (None, None)."""
    for run in runs:
        job = find_job(fetch_run_jobs(run["databaseId"], repo, env), job_name)
        if job:
            return run, job
    return None, None


def job_summary_line(
    repo: str, branch: str | None, runs: list[dict[str, Any]], job_name: str, state: str,
    run: dict[str, Any] | None,
) -> str:
    """CI o/r@main 1a2b3c4: job build success (run still in_progress)"""
    sha = (runs[0].get("headSha") or "")[:7] if runs else ""
    where = f"{repo}@{branch or (runs[0].get('headBranch') if runs else '')}"
    if run is None:
        tail = ("not in " if is_done(runs) else "not yet in ") + ", ".join(run_label(r) for r in runs)
    elif run.get("status") == "completed":
        tail = f"run {run.get('conclusion') or 'completed'}"
    else:
        tail = f"run still {run.get('status') or 'pending'}"
    return f"CI {where} {sha}: job {job_name} {state} ({tail})"


def ghci_job(env: dict[str, str], args: argparse.Namespace, runs: list[dict[str, Any]], started: float) -> int:
    """--job: report (status) or wait for (watch) that one job, in whichever of `runs` has it."""
    if args.mode == "watch":
        print(f"🔍 Waiting on job {args.job} in {len(runs)} run(s): {', '.join(run_label(r) for r in runs)}")
    logged: set[tuple[str, ...]] = set()
    while True:
        run, job = locate_job(env, args.repo, runs, args.job)
        if job and (args.log_file or args.steps):
            log_new_successes([job], logged, args.log_file, args.steps, f"{run_label(run)} / ")
        if job and job.get("status") == "completed":
            break
        if not job and is_done(runs):  # every run is over and none had it
            print(job_summary_line(args.repo, args.branch, runs, args.job, "not found", None))
            print(f"ERROR: no job named {args.job!r} in {', '.join(run_label(r) for r in runs)}", file=sys.stderr)
            return EXIT_ERROR
        if args.mode == "status":
            print_runs([run] if run else runs, env, args.repo, args.job, args.steps)
            state = "running" if job else "not started"
            print(job_summary_line(args.repo, args.branch, runs, args.job, state, run))
            return EXIT_RUNNING
        if args.timeout and time.monotonic() - started > args.timeout:
            print(job_summary_line(args.repo, args.branch, runs, args.job, "timeout", run))
            return EXIT_TIMEOUT
        time.sleep(args.poll_seconds)
        runs = [view_run(env, args.repo, r["databaseId"]) for r in runs]

    assert run is not None and job is not None
    conclusion = job.get("conclusion") or "completed"
    print(f"{format_job({**run, 'name': run_label(run)})}  {run.get('url', '')}")
    print(f"   {format_job(job)}")
    for step in job.get("steps") or []:
        if args.steps or step.get("conclusion") in ("failure", "cancelled"):
            print(f"      {format_job(step)}")
    if conclusion not in OK_CONCLUSIONS and args.log_lines:
        print(f"\n--- {run_label(run)} / {args.job}: last {args.log_lines} lines of the failed steps ---")
        print(failed_log_tail(env, args.repo, run["databaseId"], args.log_lines, job.get("databaseId")))
    print(job_summary_line(args.repo, args.branch, runs, args.job, conclusion, run))
    return EXIT_OK if conclusion in OK_CONCLUSIONS else EXIT_FAILED


# --- CLI --------------------------------------------------------------------------------------------


def complete_repos(prefix: str, **_: Any) -> list[str]:
    owner = prefix.split("/")[0] if "/" in prefix else default_owner()
    if not owner:
        return []
    try:
        out = gh_text(cli_env(), "repo", "list", owner, "--limit", "200",
                      "--json", "nameWithOwner", "--jq", ".[].nameWithOwner")
    except Exception:
        return []
    return [r for r in out.split() if r.startswith(prefix) or r.split("/")[-1].startswith(prefix)]


def complete_branches(prefix: str, parsed_args: argparse.Namespace, **_: Any) -> list[str]:
    repo = full_repo(parsed_args.repo) if getattr(parsed_args, "repo", None) else current_repo()
    if not repo:
        return []
    try:
        out = gh_text(cli_env(), "api", f"repos/{repo}/branches?per_page=100", "--jq", ".[].name")
    except Exception:
        return []
    return [b for b in out.split() if b.startswith(prefix)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghci",
        description="Check or wait for GitHub Actions CI. Exit 0 ok, 1 failed, 2 error, 3 running, 124 timeout.",
    )
    parser.add_argument("mode", nargs="?", choices=("watch", "status"), default="watch",
                        help="watch (default): wait until done; status: report now")
    repo_arg = parser.add_argument("--repo", "-r", help="owner/name or name (default: the cwd repo)")
    branch_arg = parser.add_argument("--branch", "-b", help="default: the cwd branch")
    parser.add_argument("--run", help="this run id only")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--commit", "-c", help="runs for this commit sha")
    target.add_argument("--head", action="store_true", help="runs for local HEAD (e.g. right after a push)")
    parser.add_argument("--workflow", "-w", help="only this workflow (name or file)")
    parser.add_argument("--job", "-j", help="just this job (exact name, in whichever run has it): watch returns as soon as it "
                             "finishes, exit code from its conclusion; 2 if no run has it")
    parser.add_argument("--user", "-u", help="only runs triggered by this user")
    parser.add_argument("--steps", action="store_true", help="show every step, and log steps as they succeed")
    parser.add_argument("--log-file", help="append jobs (and with --steps, steps) as they succeed")
    parser.add_argument("--log-lines", type=int, default=40, help="failed-log lines to show (0: none)")
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--appear-seconds", type=int, default=180,
                        help="with --head/--commit, how long to wait for runs to show up")
    parser.add_argument("--timeout", type=int, default=0, help="give up after this many seconds (exit 124)")
    parser.add_argument("--require-success", action="store_true", help=argparse.SUPPRESS)  # old flag: now always
    repo_arg.completer = complete_repos  # type: ignore[attr-defined]
    branch_arg.completer = complete_branches  # type: ignore[attr-defined]
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        import argcomplete

        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args(argv)

    in_cwd_repo = args.in_cwd_repo = not args.repo
    args.repo = full_repo(args.repo) if args.repo else current_repo()
    if not args.repo:
        parser.error("not in a GitHub repo: pass --repo")
    # For finding the branch's PR on the base repo (a fork clone's upstream remote).
    args.cwd_upstream = remote_repo("upstream") if in_cwd_repo else None
    args.pr_branch = git_out("branch", "--show-current") if in_cwd_repo else None
    if args.head:
        if not in_cwd_repo:
            parser.error("--head is the cwd repo's HEAD: drop --repo, or pass --commit")
        args.commit = git_out("rev-parse", "HEAD")
    if not args.branch and in_cwd_repo and not args.commit:
        args.branch = git_out("branch", "--show-current")
    if not (args.branch or args.commit or args.run):
        parser.error("pass --branch, --commit or --run (no current branch here)")

    try:
        return ghci(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
