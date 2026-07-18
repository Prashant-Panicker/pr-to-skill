"""
Pulls every closed PR in a set of repos and every comment/review the target
person left on them, using the already-authenticated `gh` CLI.

Why `gh api` instead of a Python GitHub client: it rides on whatever auth
you've already set up (`gh auth login`), handles pagination cleanly with
--paginate, and needs zero extra credential plumbing.
"""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from typing import Optional


# Transport failures can occur while `gh` is reading a GitHub API response.
# Keep this deliberately limited to failures that are safe to retry: an
# authentication or validation error must still fail immediately.
_TRANSIENT_GH_ERROR_MARKERS = (
    "eof",
    "connection reset",
    "connection refused",
    "timeout",
    "temporary failure",
    "broken pipe",
    "http2 stream error",
    "server error",
    "status 502",
    "status 503",
    "status 504",
)


def _parse_json_documents(text: str) -> list[list | dict]:
    """Decode the one-or-more JSON documents emitted by ``gh api --paginate``."""
    decoder = json.JSONDecoder()
    documents: list[list | dict] = []
    position = 0

    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        document, position = decoder.raw_decode(text, position)
        if not isinstance(document, (list, dict)):
            raise ValueError("gh returned a JSON value that was not an array or object")
        documents.append(document)

    return documents


def _is_transient_gh_error(stderr: str) -> bool:
    """Return whether a failed `gh` invocation is safe to retry."""
    return any(marker in stderr.lower() for marker in _TRANSIENT_GH_ERROR_MARKERS)


def run_gh(args: list[str], max_retries: int = 3) -> list | dict:
    """Run a `gh` command and return parsed JSON, retrying transient failures."""
    cmd = ["gh"] + args
    
    for attempt in range(max_retries):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            # Check for transient network errors in stderr
            if result.returncode != 0:
                if _is_transient_gh_error(result.stderr) and attempt < max_retries - 1:
                    # Exponential backoff: 1s, 2s, 4s
                    wait_time = 2 ** attempt
                    print(f"  [Retry {attempt + 1}/{max_retries}] Transient error, retrying in {wait_time}s...", file=sys.stderr)
                    time.sleep(wait_time)
                    continue
                
                raise RuntimeError(f"gh command failed: {' '.join(cmd)}\n{result.stderr}")
            
            text = result.stdout.strip()
            if not text:
                return []

            documents = _parse_json_documents(text)
            if "--paginate" not in args:
                if len(documents) != 1:
                    raise ValueError(f"gh returned {len(documents)} JSON documents without --paginate")
                return documents[0]

            flattened: list = []
            for page_number, page in enumerate(documents, start=1):
                if not isinstance(page, list):
                    raise ValueError(
                        f"paginated gh response page {page_number} was not a JSON array"
                    )
                flattened.extend(page)
            return flattened
            
        except subprocess.TimeoutExpired:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"  [Retry {attempt + 1}/{max_retries}] API timeout, retrying in {wait_time}s...", file=sys.stderr)
                time.sleep(wait_time)
                continue
            raise RuntimeError(f"gh command timed out after {max_retries} attempts: {' '.join(cmd)}")
    
    # Should not reach here, but just in case
    raise RuntimeError(f"gh command failed after {max_retries} attempts: {' '.join(cmd)}")


@dataclass
class RawComment:
    repo: str
    pr_number: int
    pr_title: str
    pr_url: str
    pr_state: str
    comment_type: str          # "review_comment" | "review_summary" | "issue_comment"
    file_path: Optional[str]
    diff_hunk: Optional[str]
    body: str
    review_state: Optional[str]  # APPROVED / CHANGES_REQUESTED / COMMENTED, if applicable
    created_at: str
    html_url: str


def list_closed_prs(repo: str, updated_after: Optional[str] = None) -> list[dict]:
    """List all closed PRs (includes merged) for a repo, paginated."""
    endpoint = f"repos/{repo}/pulls?state=closed&per_page=100&sort=updated&direction=desc"
    prs = run_gh(["api", "--paginate", endpoint])
    if updated_after:
        prs = [p for p in prs if p["updated_at"] >= updated_after]
    return prs


def get_pr_review_comments(repo: str, pr_number: int) -> list[dict]:
    """Line-level review comments (the ones with diff_hunk/path)."""
    endpoint = f"repos/{repo}/pulls/{pr_number}/comments?per_page=100"
    return run_gh(["api", "--paginate", endpoint])


def get_pr_reviews(repo: str, pr_number: int) -> list[dict]:
    """Review summaries (APPROVE / REQUEST_CHANGES / COMMENT with a body)."""
    endpoint = f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100"
    return run_gh(["api", "--paginate", endpoint])


def get_pr_issue_comments(repo: str, pr_number: int) -> list[dict]:
    """General (non-line-specific) PR conversation comments."""
    endpoint = f"repos/{repo}/issues/{pr_number}/comments?per_page=100"
    return run_gh(["api", "--paginate", endpoint])


def _collect_for_pr(repo: str, username: str, pr: dict, progress_cb=None) -> list[RawComment]:
    out: list[RawComment] = []
    pr_number = pr["number"]
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "")
    pr_state = "merged" if pr.get("merged_at") else "closed"

    if progress_cb:
        progress_cb(repo, pr_number, pr_title)

    # 1. Line-level review comments
    for c in get_pr_review_comments(repo, pr_number):
        if c.get("user", {}).get("login") != username:
            continue
        out.append(RawComment(
            repo=repo, pr_number=pr_number, pr_title=pr_title, pr_url=pr_url,
            pr_state=pr_state, comment_type="review_comment",
            file_path=c.get("path"), diff_hunk=c.get("diff_hunk"),
            body=c.get("body", ""), review_state=None,
            created_at=c.get("created_at", ""), html_url=c.get("html_url", ""),
        ))

    # 2. Review summaries (the top-level "Changes requested" / "Approved" body)
    for r in get_pr_reviews(repo, pr_number):
        if r.get("user", {}).get("login") != username:
            continue
        if not r.get("body"):
            continue  # skip empty-body approvals, nothing to learn from
        out.append(RawComment(
            repo=repo, pr_number=pr_number, pr_title=pr_title, pr_url=pr_url,
            pr_state=pr_state, comment_type="review_summary",
            file_path=None, diff_hunk=None,
            body=r.get("body", ""), review_state=r.get("state"),
            created_at=r.get("submitted_at", ""), html_url=r.get("html_url", ""),
        ))

    # 3. General conversation comments
    for c in get_pr_issue_comments(repo, pr_number):
        if c.get("user", {}).get("login") != username:
            continue
        out.append(RawComment(
            repo=repo, pr_number=pr_number, pr_title=pr_title, pr_url=pr_url,
            pr_state=pr_state, comment_type="issue_comment",
            file_path=None, diff_hunk=None,
            body=c.get("body", ""), review_state=None,
            created_at=c.get("created_at", ""), html_url=c.get("html_url", ""),
        ))

    return out


def collect_for_repo(repo: str, username: str, updated_after: Optional[str] = None,
                     max_workers: int = 8, progress_cb=None) -> list[RawComment]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    prs = list_closed_prs(repo, updated_after)

    def collect(pr: dict) -> list[RawComment]:
        return _collect_for_pr(repo, username, pr, progress_cb)

    # executor.map preserves PR order even though network calls run concurrently.
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="github") as executor:
        per_pr_comments = executor.map(collect, prs)
        return [comment for comments in per_pr_comments for comment in comments]


def collect_all(repos: list[str], username: str, updated_after: Optional[str] = None,
                max_workers: int = 8) -> list[RawComment]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    all_comments: list[RawComment] = []
    for repo in repos:
        def progress_cb(repo, pr_number, title):
            print(f"  scanning {repo} PR #{pr_number}: {title[:60]}", file=sys.stderr)
        print(f"Scanning repo: {repo} ({max_workers} workers)", file=sys.stderr)
        all_comments.extend(
            collect_for_repo(
                repo,
                username,
                updated_after,
                max_workers=max_workers,
                progress_cb=progress_cb,
            )
        )
    return all_comments


def save_raw(comments: list[RawComment], path: str):
    with open(path, "w") as f:
        json.dump([asdict(c) for c in comments], f, indent=2)
