# ghci

Two small command-line tools for GitHub, built on the `gh` CLI:

- **`ghci`** checks or waits for GitHub Actions CI on any repo and ends with one summary line (and, on failure, the
  failed jobs and the end of their log). It's meant for scripts and coding agents as much as for people: exit
  codes say what happened, and nobody has to poll `gh run list`.
- **`ghclean`** audits and cleans up your open PRs, your forks' branches and the git clones on your machine.

Each one is a single stdlib-only Python file (`src/ghci/actions.py`, `src/ghci/clean.py`) that imports nothing
else from the package, so you can also copy one anywhere and run it as a script.

## Install

```bash
uv tool install "ghci[completion] @ git+https://github.com/maaroofunbxd/ghci"   # or: pip install git+https://...
```

You need Python 3.9 or newer, and `gh` logged in (`gh auth login`) or `GH_TOKEN` / `GITHUB_TOKEN` set. `ghclean` also
needs git 2.38 or newer. The `completion` extra adds argcomplete, for tab completion of `ghci` options and repos
(`eval "$(register-python-argcomplete ghci)"`). To use it as a dependency:
`ghci @ git+https://github.com/maaroofunbxd/ghci@v0.1.0`.

## ghci

```bash
ghci status                      # CI state of the current repo's branch tip, no waiting (exit 3 = still running)
ghci                             # wait for it; exit 0 on success
ghci --head                      # after a push: CI for local HEAD (waits for the runs to appear)
ghci -r octo/app -b feature -j "Build and push"   # one job only; returns once that job is done
ghci --run 123456789 --steps --log-file ~/ci.log
ghci -h                          # everything else: --workflow, --commit, --user, --timeout, ...
```

A bare repo name (`-r app`) is taken to be in the current clone's owner, else in `$GHCI_OWNER`.

The last line is always one summary line, for example `CI octo/app@feature 1a2b3c4: success (2/2 runs ok)`.
Exit codes: 0 success, 1 failed or cancelled, 2 error (bad arguments, a gh error, no runs), 3 still running
(`status` only), 124 `--timeout` reached.

For a branch with an open PR from a fork, ghci follows the PR's CI on the base repo. When the token can't read the
Actions API (HTTP 404), it falls back to the PR's checks (`gh pr checks`).

## ghclean

```bash
ghclean prs                                      # your open PRs: age, CI, review, mergeable (--json for scripts)
ghclean close octo/app#225 -m "why"              # preview; add --yes to close
ghclean clones --root ~                          # clones under ~: dirty files, unpushed branches, tokens in remote URLs
ghclean remotes --fix                            # rewrite remotes with credentials in the URL to SSH
ghclean branches ~/api                           # local and fork branches: merged / content upstream / unmerged
ghclean branches ~/api --delete-local --delete-fork --yes   # delete the merged ones
ghclean preserve ~/web some-branch               # push laptop-only branches as preserve/<branch>
```

Never deleted: the default branch, branches checked out in any worktree, heads of your open PRs, and `--keep`
patterns (default `preserve/*`, `*backup*`). Every deleted branch is appended to `~/.ghclean-deleted.log` with its
sha, so you can restore it with `git branch <b> <sha>`. `ghclean -h` has the details.

## Development

```bash
uv sync
uv run ruff check src tests
uv run pytest -q
```

CI runs both on Python 3.9 and 3.14 (`.github/workflows/tests.yml`).
