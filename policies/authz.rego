package mcp.authz
default allow = false

# Example: allow guarded write endpoints only with a shared header.
allow {
  input.method == "POST"
  some path
  path := input.path
  (path == "/open_pr" or path == "/create_jira")
  input.headers["x-ci-token"] == data.allowed_tokens.ci
}

# Allow everything else (read-only)
allow {
  not startswith(input.path, "/open_pr")
  not startswith(input.path, "/create_jira")
}
