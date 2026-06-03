# git-shared

GitPython-backed adapter used by the admin-dashboard-api `PromptsGitRouter`
to drive the prompt CRUD / draft branch / PR flow.

The library is deliberately tiny — it owns nothing more than:

- `GitRepo` — a thin wrapper over `git.Repo` with the four methods the
  router needs: `read_file(path, branch)`, `create_branch_from_main(name)`,
  `write_file(path, body, branch)`, `commit(branch, message, author)`,
  `diff(branch, against)`.
- `PullRequestOpener` — abstract protocol for opening a PR. The
  in-package `BitbucketPullRequestOpener` adapter wraps a callable
  (typically an `mcp_client` `bitbucket_create_pull_request_cloud` tool
  invocation) and is the single integration point with Bitbucket.

The wrapper deliberately keeps the working tree untouched: every write
goes to a side-branch checked out in a detached worktree-like state so
the router never mutates `main`. The router and tests cover the
RBAC / audit / template-format-validation surface; this lib stays
behaviour-only.
