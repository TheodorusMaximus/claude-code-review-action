#!/usr/bin/env python3
"""
Claude-powered PR review engine.

Generic across any repo. Reads .claude/CLAUDE.md for project conventions
if present. Optionally reads vault context from a notes repo checkout.

Environment variables required:
  ANTHROPIC_API_KEY     — Claude API key
  GITHUB_TOKEN          — GitHub token (provided by Actions)
  PR_NUMBER             — Pull request number
  REPO                  — owner/repo (e.g., user/my-project)

Optional:
  REVIEW_MODEL          — Claude model (default: claude-sonnet-4-6-20250514)
  CONFIDENCE_THRESHOLD  — Minimum confidence to post (default: 80)
  MAX_DIFF_SIZE         — Max diff bytes before skipping (default: 100000)
  VAULT_CONTEXT_FILE    — Path to vault context file (from notes repo checkout)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import anthropic


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL = os.environ.get("REVIEW_MODEL", "claude-sonnet-4-6-20250514")
CONFIDENCE_THRESHOLD = int(os.environ.get("CONFIDENCE_THRESHOLD", "80"))
MAX_DIFF_SIZE = int(os.environ.get("MAX_DIFF_SIZE", "100000"))
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]


# ---------------------------------------------------------------------------
# GitHub helpers (via gh CLI)
# ---------------------------------------------------------------------------


def gh_api(
    endpoint: str, method: str = "GET", body: dict | None = None
) -> dict | list:
    """Call GitHub API via gh CLI."""
    cmd = ["gh", "api", endpoint, "--method", method]
    if body:
        cmd += ["--input", "-"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=json.dumps(body) if body else None,
        check=False,
    )
    if result.returncode != 0:
        print(f"gh api error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def get_pr_info() -> dict:
    """Fetch PR metadata."""
    return gh_api(f"/repos/{REPO}/pulls/{PR_NUMBER}")


def get_pr_diff() -> str:
    """Fetch the PR diff."""
    result = subprocess.run(
        [
            "gh", "api", f"/repos/{REPO}/pulls/{PR_NUMBER}",
            "--header", "Accept: application/vnd.github.v3.diff",
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"Failed to fetch diff: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def get_changed_files() -> list[dict]:
    """Fetch list of changed files with patch info."""
    files: list[dict] = []
    page = 1
    while True:
        batch = gh_api(
            f"/repos/{REPO}/pulls/{PR_NUMBER}/files?per_page=100&page={page}"
        )
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def post_review(body: str, event: str, comments: list[dict]) -> None:
    """Post a pull request review with inline comments."""
    payload: dict = {"body": body, "event": event}
    if comments:
        payload["comments"] = comments
    gh_api(
        f"/repos/{REPO}/pulls/{PR_NUMBER}/reviews",
        method="POST",
        body=payload,
    )


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------


def load_project_conventions() -> str:
    """Read .claude/CLAUDE.md if it exists in the checkout."""
    claude_md = Path(".claude/CLAUDE.md")
    if claude_md.exists():
        return claude_md.read_text()
    return ""


def load_vault_context() -> str:
    """Read vault context file if provided via env var."""
    path = os.environ.get("VAULT_CONTEXT_FILE", "")
    if path and Path(path).exists():
        return Path(path).read_text()
    return ""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert code reviewer. You review pull requests with precision \
and focus on issues that genuinely matter.

## Review Philosophy
- Only flag issues you are genuinely confident about (>= {confidence}% confidence)
- Skip anything CI already catches: formatting, linting, type errors, test failures
- Focus on: logic errors, security issues, performance problems, missing edge cases, \
architecture concerns, and correctness
- Be specific: reference exact lines, explain why it's a problem, suggest a fix
- Be concise: one issue per comment, no filler

## Severity Levels
- **critical**: Will cause bugs, data loss, security vulnerabilities, or crashes
- **important**: Likely to cause issues, poor patterns, or significant maintainability concerns
- **minor**: Style preferences, small improvements, or optional suggestions

## Output Format
Return ONLY a JSON object (no markdown fences) with this exact structure:
{{
  "summary": "1-2 sentence overall assessment",
  "verdict": "approve" | "request_changes" | "comment",
  "issues": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "severity": "critical" | "important" | "minor",
      "confidence": 85,
      "title": "Short issue title",
      "body": "Detailed explanation and suggested fix"
    }}
  ]
}}

Rules for verdict:
- "request_changes" if ANY critical issue exists
- "comment" if only important/minor issues exist
- "approve" if no issues or only very minor suggestions

Only include issues with confidence >= {confidence}. If nothing is worth flagging, \
return an empty issues array and verdict "approve".
"""


def build_user_prompt(
    pr_info: dict,
    diff: str,
    conventions: str,
    vault_context: str,
) -> str:
    """Build the user prompt with PR context."""
    parts: list[str] = []

    parts.append(f"## Pull Request\n**Title:** {pr_info['title']}")
    if pr_info.get("body"):
        parts.append(f"**Description:**\n{pr_info['body']}")

    if conventions:
        parts.append(
            "## Project Conventions (from .claude/CLAUDE.md)\n"
            f"```\n{conventions}\n```"
        )

    if vault_context:
        parts.append(
            "## Project Notes (from knowledge base)\n"
            f"```\n{vault_context}\n```"
        )

    parts.append(f"## Diff\n```diff\n{diff}\n```")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------


def call_claude(system: str, user: str) -> dict:
    """Call Claude API and parse the JSON response."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    text = response.content[0].text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]

    return json.loads(text.strip())


# ---------------------------------------------------------------------------
# Review posting
# ---------------------------------------------------------------------------

VERDICT_MAP = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}


def build_review_body(result: dict, filtered_count: int) -> str:
    """Build the review summary body."""
    parts = ["## Claude Code Review\n"]
    parts.append(result["summary"])

    issues = result.get("issues", [])
    if issues:
        critical = sum(1 for i in issues if i["severity"] == "critical")
        important = sum(1 for i in issues if i["severity"] == "important")
        minor = sum(1 for i in issues if i["severity"] == "minor")
        counts = []
        if critical:
            counts.append(f"{critical} critical")
        if important:
            counts.append(f"{important} important")
        if minor:
            counts.append(f"{minor} minor")
        parts.append(f"\n**Issues:** {', '.join(counts)}")

    if filtered_count:
        parts.append(f"\n*({filtered_count} low-confidence suggestions omitted)*")

    parts.append(
        f"\n---\n*Reviewed by Claude ({MODEL}) "
        f"| confidence >= {CONFIDENCE_THRESHOLD}%*"
    )
    return "\n".join(parts)


def format_inline_comments(
    issues: list[dict], changed_files: list[dict]
) -> list[dict]:
    """Convert issues to GitHub review comments, validating against changed files."""
    valid_files = {f["filename"] for f in changed_files}
    comments = []
    for issue in issues:
        if issue["file"] not in valid_files:
            continue
        body = (
            f"**{issue['severity'].upper()}** "
            f"({issue['confidence']}% confidence): "
            f"{issue['title']}\n\n{issue['body']}"
        )
        comments.append({
            "path": issue["file"],
            "line": issue["line"],
            "body": body,
        })
    return comments


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"Reviewing PR #{PR_NUMBER} in {REPO}")

    # Fetch PR data
    pr_info = get_pr_info()

    # Skip draft PRs
    if pr_info.get("draft"):
        print("Skipping draft PR")
        return

    # Skip bot PRs
    user_login = pr_info.get("user", {}).get("login", "")
    if user_login.endswith("[bot]") or user_login in (
        "dependabot", "renovate", "github-actions",
    ):
        print(f"Skipping bot PR (author: {user_login})")
        return

    # Fetch diff
    diff = get_pr_diff()
    diff_size = len(diff.encode())
    if diff_size > MAX_DIFF_SIZE:
        print(f"Skipping: diff too large ({diff_size} bytes > {MAX_DIFF_SIZE})")
        return

    if not diff.strip():
        print("Skipping: empty diff")
        return

    # Load context
    conventions = load_project_conventions()
    vault_context = load_vault_context()

    if conventions:
        print("Loaded project conventions from .claude/CLAUDE.md")
    if vault_context:
        print("Loaded vault context from knowledge base")

    # Build prompt
    system = SYSTEM_PROMPT.format(confidence=CONFIDENCE_THRESHOLD)
    user_prompt = build_user_prompt(pr_info, diff, conventions, vault_context)

    # Call Claude
    print(f"Calling {MODEL}...")
    try:
        result = call_claude(system, user_prompt)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"Failed to parse Claude response: {e}", file=sys.stderr)
        sys.exit(1)

    # Filter by confidence
    all_issues = result.get("issues", [])
    filtered_issues = [
        i for i in all_issues if i.get("confidence", 0) >= CONFIDENCE_THRESHOLD
    ]
    filtered_count = len(all_issues) - len(filtered_issues)
    result["issues"] = filtered_issues

    # Determine verdict based on filtered issues
    has_critical = any(i["severity"] == "critical" for i in filtered_issues)
    if has_critical:
        verdict = "request_changes"
    elif filtered_issues:
        verdict = "comment"
    else:
        verdict = "approve"

    # Build and post review
    changed_files = get_changed_files()
    comments = format_inline_comments(filtered_issues, changed_files)
    body = build_review_body(result, filtered_count)
    event = VERDICT_MAP[verdict]

    print(f"Posting review: {event} with {len(comments)} inline comments")
    post_review(body, event, comments)
    print("Review posted successfully.")


if __name__ == "__main__":
    main()
