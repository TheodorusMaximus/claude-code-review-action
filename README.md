# Claude Code Review Action

Claude-powered PR code review with confidence-based filtering. Designed as a developer-level tool that works across any project.

## Features

- **Confidence-based filtering** — only posts issues Claude is genuinely sure about (default >= 80%)
- **Project-aware** — reads `.claude/CLAUDE.md` for project conventions (if present)
- **Knowledge base integration** — optionally pulls context from an external notes repo (e.g., Obsidian vault backup)
- **Smart skipping** — ignores draft PRs, bot PRs (dependabot/renovate), and oversized diffs
- **Proper GitHub reviews** — posts as a real review with inline comments, not just PR comments

## Quick Start

Add this to `.github/workflows/pr-review.yml` in any repo:

```yaml
name: Claude PR Review

on:
  pull_request:
    types: [opened, ready_for_review, synchronize]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    if: github.event.pull_request.draft == false
    steps:
      - uses: actions/checkout@v4

      - uses: TheodorusMaximus/claude-code-review-action@main
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
```

## With Knowledge Base (Obsidian Vault)

If you back up your Obsidian vault to a repo, the action can pull project-specific notes:

```yaml
      - uses: TheodorusMaximus/claude-code-review-action@main
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
          context_repo: "YourUser/your-vault-repo"
          context_repo_token: ${{ secrets.VAULT_TOKEN }}  # if private
          # Auto-detects Projects/<repo-name>.md, or specify:
          # context_file_path: "Projects/MyProject.md"
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `anthropic_api_key` | Yes | — | Anthropic API key |
| `github_token` | Yes | — | GitHub token for API calls |
| `model` | No | `claude-sonnet-4-20250514` | Claude model to use |
| `confidence_threshold` | No | `80` | Min confidence (0-100) to post issues |
| `max_diff_size` | No | `100000` | Max diff bytes before skipping |
| `context_repo` | No | — | Repo with project notes (e.g., `user/vault`) |
| `context_repo_token` | No | `github_token` | Token for private context repo |
| `context_file_path` | No | auto-detect | Path within context repo to read |

## How It Works

1. Fetches the PR diff and metadata via GitHub API
2. Reads `.claude/CLAUDE.md` from the repo (if present) for project conventions
3. Optionally reads project notes from an external knowledge base repo
4. Sends everything to Claude with a structured review prompt
5. Filters response by confidence threshold
6. Posts a GitHub review with inline comments

### Verdict Logic

- **APPROVE** — no issues found (or all below confidence threshold)
- **COMMENT** — only important/minor issues
- **REQUEST_CHANGES** — at least one critical issue

### What It Skips

- Draft PRs
- Bot PRs (dependabot, renovate, github-actions)
- Diffs larger than `max_diff_size`
- Anything CI catches (formatting, lint, types, test failures)

## Secrets Setup

```bash
# Required — your Anthropic API key
gh secret set ANTHROPIC_API_KEY

# Optional — if your vault repo is private
gh secret set VAULT_TOKEN
```

## Context Resolution

When `context_repo` is provided, the action looks for context in this order:

1. `context_file_path` if explicitly set
2. `Projects/<repo-name>.md` (auto-detected from the repository name)

This works well with Obsidian vaults that have a `Projects/` folder with per-project notes.
