"""
Pulls every closed PR in a set of repos and every comment/review the target
person left on them, using the already-authenticated `gh` CLI.

Why `gh api` instead of a Python GitHub client: it rides on whatever auth
you've already set up (`gh auth login`), handles pagination cleanly with
--paginate, and needs zero extra credential plumbing.
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from typing import Optional

import jwt
import requests


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
MAX_FINAL_DIFF_CHARS = 500_000


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


class _GitHubTokenProvider:
    def __init__(self):
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self) -> str:
        configured_token = os.environ.get("GITHUB_TOKEN")
        if configured_token:
            return configured_token
        if self._token and time.time() < self._expires_at - 300:
            return self._token
        app_id = os.environ.get("GITHUB_APP_ID")
        installation_id = os.environ.get("GITHUB_INSTALLATION_ID")
        private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n")
        if not app_id or not installation_id or not private_key:
            raise ValueError(
                "GitHub HTTP access requires GITHUB_TOKEN or GitHub App credentials"
            )
        now = int(time.time())
        app_token = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": app_id},
            private_key,
            algorithm="RS256",
        )
        response = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["token"]
        self._expires_at = time.time() + 3300
        return self._token


_TOKEN_PROVIDER = _GitHubTokenProvider()


def _run_github_api(args: list[str], max_retries: int) -> list | dict:
    paginate = "--paginate" in args
    method = "GET"
    fields: dict[str, str] = {}
    endpoint = None
    index = 1
    while index < len(args):
        argument = args[index]
        if argument == "--paginate":
            index += 1
        elif argument == "--method":
            method = args[index + 1]
            index += 2
        elif argument == "-f":
            key, value = args[index + 1].split("=", 1)
            fields[key] = value
            index += 2
        else:
            endpoint = argument
            index += 1
    if endpoint is None:
        raise ValueError("GitHub API endpoint is required")

    url = f"https://api.github.com/{endpoint.lstrip('/')}"
    collected: list = []
    while url:
        for attempt in range(max_retries):
            response = requests.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {_TOKEN_PROVIDER.get()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=fields or None,
                timeout=60,
            )
            if response.status_code not in {429, 502, 503, 504}:
                response.raise_for_status()
                break
            if attempt == max_retries - 1:
                response.raise_for_status()
            time.sleep(2 ** attempt)
        payload = response.json()
        if not paginate:
            return payload
        if not isinstance(payload, list):
            raise ValueError("Paginated GitHub response must be an array")
        collected.extend(payload)
        url = response.links.get("next", {}).get("url")
    return collected


def run_gh(args: list[str], max_retries: int = 3) -> list | dict:
    """Run a `gh` command and return parsed JSON, retrying transient failures."""
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_APP_ID"):
        return _run_github_api(args, max_retries)
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
    github_comment_id: int
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
    reviewer: str
    replies: list[dict]
    final_diff: str
    merged_at: str


def list_closed_prs(repo: str, updated_after: Optional[str] = None) -> list[dict]:
    """List merged PRs for a repo; unmerged feedback cannot become guidance."""
    endpoint = f"repos/{repo}/pulls?state=closed&per_page=100&sort=updated&direction=desc"
    prs = run_gh(["api", "--paginate", endpoint])
    if updated_after:
        prs = [p for p in prs if p["updated_at"] >= updated_after]
    return [pull_request for pull_request in prs if pull_request.get("merged_at")]


def get_pull_request(repo: str, pr_number: int) -> dict:
    return run_gh(["api", f"repos/{repo}/pulls/{pr_number}"])


def get_pull_request_files(repo: str, pr_number: int) -> list[dict]:
    endpoint = f"repos/{repo}/pulls/{pr_number}/files?per_page=100"
    return run_gh(["api", "--paginate", endpoint])


def get_pull_request_diff(repo: str, pr_number: int) -> str:
    endpoint = f"repos/{repo}/pulls/{pr_number}"
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_APP_ID"):
        response = requests.get(
            f"https://api.github.com/{endpoint}",
            headers={
                "Authorization": f"Bearer {_TOKEN_PROVIDER.get()}",
                "Accept": "application/vnd.github.diff",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=120,
        )
        response.raise_for_status()
        diff = response.text
    else:
        result = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.diff", endpoint],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh command failed while fetching PR diff: {result.stderr}")
        diff = result.stdout
    if not diff.strip():
        raise ValueError(f"GitHub returned an empty final diff for {repo} PR #{pr_number}")
    if len(diff) > MAX_FINAL_DIFF_CHARS:
        raise ValueError(
            f"Final diff for {repo} PR #{pr_number} is {len(diff)} characters; "
            f"maximum supported is {MAX_FINAL_DIFF_CHARS}"
        )
    return diff


class GitHubReviewPublisher:
    def publish(self, repo: str, pr_number: int, body: str, head_sha: str) -> str:
        marker = f"<!-- pr-to-skill:{head_sha} -->"
        reviews = get_pr_reviews(repo, pr_number)
        for review in reviews:
            if marker in (review.get("body") or ""):
                return review.get("html_url", "")
        response = run_gh([
            "api", "--method", "POST", f"repos/{repo}/pulls/{pr_number}/reviews",
            "-f", f"body={body}\n\n{marker}", "-f", "event=COMMENT",
            "-f", f"commit_id={head_sha}",
        ])
        return response.get("html_url", "")


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


def _trusted_usernames(usernames: str | list[str]) -> set[str]:
    values = [usernames] if isinstance(usernames, str) else usernames
    trusted = {value.lower() for value in values if isinstance(value, str) and value}
    if not trusted:
        raise ValueError("At least one trusted GitHub username is required")
    return trusted


def _thread_replies(root_id: int, comments_by_parent: dict[int, list[dict]]) -> list[dict]:
    replies: list[dict] = []
    pending = list(comments_by_parent.get(root_id, []))
    while pending:
        reply = pending.pop(0)
        replies.append({
            "github_comment_id": reply["id"],
            "author": reply.get("user", {}).get("login", ""),
            "body": reply.get("body", ""),
            "created_at": reply.get("created_at", ""),
            "html_url": reply.get("html_url", ""),
        })
        pending.extend(comments_by_parent.get(reply["id"], []))
    return replies


def _collect_for_pr(
    repo: str, usernames: str | list[str], pr: dict, progress_cb=None
) -> list[RawComment]:
    out: list[RawComment] = []
    trusted = _trusted_usernames(usernames)
    pr_number = pr["number"]
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "")
    merged_at = pr.get("merged_at") or ""
    pr_state = "merged" if merged_at else pr.get("state", "open")
    final_diff = get_pull_request_diff(repo, pr_number) if merged_at else ""

    if progress_cb:
        progress_cb(repo, pr_number, pr_title)

    # 1. Line-level review comments
    review_comments = get_pr_review_comments(repo, pr_number)
    comments_by_parent: dict[int, list[dict]] = {}
    for comment in review_comments:
        parent_id = comment.get("in_reply_to_id")
        if parent_id is not None:
            comments_by_parent.setdefault(parent_id, []).append(comment)
    for c in review_comments:
        reviewer = c.get("user", {}).get("login", "")
        if reviewer.lower() not in trusted or c.get("in_reply_to_id") is not None:
            continue
        out.append(RawComment(
            repo=repo, pr_number=pr_number, github_comment_id=c["id"],
            pr_title=pr_title, pr_url=pr_url,
            pr_state=pr_state, comment_type="review_comment",
            file_path=c.get("path"), diff_hunk=c.get("diff_hunk"),
            body=c.get("body", ""), review_state=None,
            created_at=c.get("created_at", ""), html_url=c.get("html_url", ""),
            reviewer=reviewer, replies=_thread_replies(c["id"], comments_by_parent),
            final_diff=final_diff, merged_at=merged_at,
        ))

    # 2. Review summaries (the top-level "Changes requested" / "Approved" body)
    for r in get_pr_reviews(repo, pr_number):
        reviewer = r.get("user", {}).get("login", "")
        if reviewer.lower() not in trusted:
            continue
        if r.get("state") == "DISMISSED":
            continue
        if not r.get("body"):
            continue  # skip empty-body approvals, nothing to learn from
        out.append(RawComment(
            repo=repo, pr_number=pr_number, github_comment_id=r["id"],
            pr_title=pr_title, pr_url=pr_url,
            pr_state=pr_state, comment_type="review_summary",
            file_path=None, diff_hunk=None,
            body=r.get("body", ""), review_state=r.get("state"),
            created_at=r.get("submitted_at", ""), html_url=r.get("html_url", ""),
            reviewer=reviewer, replies=[], final_diff=final_diff, merged_at=merged_at,
        ))

    # 3. General conversation comments
    for c in get_pr_issue_comments(repo, pr_number):
        reviewer = c.get("user", {}).get("login", "")
        if reviewer.lower() not in trusted:
            continue
        out.append(RawComment(
            repo=repo, pr_number=pr_number, github_comment_id=c["id"],
            pr_title=pr_title, pr_url=pr_url,
            pr_state=pr_state, comment_type="issue_comment",
            file_path=None, diff_hunk=None,
            body=c.get("body", ""), review_state=None,
            created_at=c.get("created_at", ""), html_url=c.get("html_url", ""),
            reviewer=reviewer, replies=[], final_diff=final_diff, merged_at=merged_at,
        ))

    return out


def collect_for_repo(repo: str, usernames: str | list[str], updated_after: Optional[str] = None,
                     max_workers: int = 8, progress_cb=None) -> list[RawComment]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    prs = list_closed_prs(repo, updated_after)

    def collect(pr: dict) -> list[RawComment]:
        return _collect_for_pr(repo, usernames, pr, progress_cb)

    # executor.map preserves PR order even though network calls run concurrently.
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="github") as executor:
        per_pr_comments = executor.map(collect, prs)
        return [comment for comments in per_pr_comments for comment in comments]


def collect_for_pull_request(
    repo: str, usernames: str | list[str], pr_number: int
) -> list[RawComment]:
    """Collect current review evidence for one open or closed pull request."""
    return _collect_for_pr(repo, usernames, get_pull_request(repo, pr_number))


def collect_all(repos: list[str], usernames: str | list[str], updated_after: Optional[str] = None,
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
                usernames,
                updated_after,
                max_workers=max_workers,
                progress_cb=progress_cb,
            )
        )
    return all_comments


def collect_merged_pull_requests(
    repos: list[str], updated_after: Optional[str] = None
) -> list[dict]:
    evidence = []
    for repo in repos:
        for pull_request in list_closed_prs(repo, updated_after):
            evidence.append({
                "pull_request": pull_request,
                "final_diff": get_pull_request_diff(repo, pull_request["number"]),
            })
    return evidence


def save_raw(comments: list[RawComment], path: str):
    with open(path, "w") as f:
        json.dump([asdict(c) for c in comments], f, indent=2)
