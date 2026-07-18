"""
Stage 1 of the AI pipeline: turn raw GitHub comments into structured notes.

For every comment we ask the model to figure out, using the diff hunk (the
"stage") plus the comment body:
  - what the original code was doing / what was there before
  - what change the person was asking for
  - why they were asking for it
  - what category of engineering practice this falls under
  - how strict the ask was (blocking vs suggestion vs nitpick)

This is the "note it down" step the raw GitHub API can't give you directly —
the API only gives you the comment text and the diff hunk, not the inferred
intent.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict

SYSTEM_PROMPT = """You are analyzing a senior software engineer's historical code \
review comments to understand their engineering standards and review philosophy. \
For each comment provided, infer the following, staying strictly grounded in what \
the diff hunk and comment text actually show (do not invent details that aren't \
implied):

- category: exactly one of "api-design", "architecture", "concurrency", \
"documentation", "error-handling", "logging", "naming", "other", \
"performance", "readability", "security", "style", "testing"
- original_issue: what the code was doing before the comment (1 sentence)
- requested_change: what change is being asked for (1 sentence)
- rationale: why the reviewer wants this change, inferred from their wording \
(1 sentence)
- severity: one of "blocking", "suggestion", "nitpick"

Return a JSON object of the form {"items": [{...}, {...}]}, one item per input \
comment, in the same order as given. Do not include any text outside the JSON."""

CATEGORIES = {
    "api-design",
    "architecture",
    "concurrency",
    "documentation",
    "error-handling",
    "logging",
    "naming",
    "other",
    "performance",
    "readability",
    "security",
    "style",
    "testing",
}
SEVERITIES = {"blocking", "suggestion", "nitpick"}
REQUIRED_ITEM_FIELDS = {
    "category",
    "original_issue",
    "requested_change",
    "rationale",
    "severity",
}


@dataclass
class Note:
    repo: str
    pr_number: int
    pr_title: str
    pr_url: str
    comment_type: str
    file_path: str | None
    original_body: str
    category: str
    original_issue: str
    requested_change: str
    rationale: str
    severity: str


def _format_comment_for_prompt(c: dict, idx: int) -> str:
    lines = [f"--- Comment {idx} ---"]
    lines.append(f"Repo: {c['repo']}  PR #{c['pr_number']}: {c['pr_title']}")
    lines.append(f"Type: {c['comment_type']}" + (f"  (review_state: {c['review_state']})" if c.get("review_state") else ""))
    if c.get("file_path"):
        lines.append(f"File: {c['file_path']}")
    if c.get("diff_hunk"):
        lines.append(f"Diff hunk:\n{c['diff_hunk']}")
    lines.append(f"Comment: {c['body']}")
    return "\n".join(lines)


def analyze_batch(client, deployment: str, batch: list[dict]) -> list[Note]:
    prompt_parts = [_format_comment_for_prompt(c, i) for i, c in enumerate(batch)]
    user_prompt = "\n\n".join(prompt_parts)
    user_prompt += f"\n\nThere are {len(batch)} comments above. Return exactly {len(batch)} items in order."

    parsed = client.call_json(deployment, SYSTEM_PROMPT, user_prompt)
    items = parsed.get("items")
    if not isinstance(items, list):
        raise ValueError("analysis response field 'items' must be an array")
    if len(items) != len(batch):
        raise ValueError(
            f"analysis response returned {len(items)} items for {len(batch)} comments"
        )

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"analysis item {index} must be an object")
        missing = REQUIRED_ITEM_FIELDS - item.keys()
        if missing:
            raise ValueError(
                f"analysis item {index} is missing fields: {', '.join(sorted(missing))}"
            )
        for field_name in REQUIRED_ITEM_FIELDS:
            if not isinstance(item[field_name], str) or not item[field_name].strip():
                raise ValueError(
                    f"analysis item {index} field '{field_name}' must be a non-empty string"
                )
        if item["category"] not in CATEGORIES:
            raise ValueError(
                f"analysis item {index} has invalid category: {item['category']!r}"
            )
        if item["severity"] not in SEVERITIES:
            raise ValueError(
                f"analysis item {index} has invalid severity: {item['severity']!r}"
            )

    notes = []
    for c, item in zip(batch, items):
        notes.append(Note(
            repo=c["repo"], pr_number=c["pr_number"], pr_title=c["pr_title"],
            pr_url=c["pr_url"], comment_type=c["comment_type"], file_path=c.get("file_path"),
            original_body=c["body"],
            category=item["category"],
            original_issue=item["original_issue"],
            requested_change=item["requested_change"],
            rationale=item["rationale"],
            severity=item["severity"],
        ))
    return notes


def analyze_all(client, deployment: str, raw_comments: list[dict], batch_size: int = 8,
                max_attempts: int = 3, max_workers: int = 4, progress_cb=None) -> list[Note]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    batches = [
        (i, raw_comments[i:i + batch_size])
        for i in range(0, len(raw_comments), batch_size)
    ]

    def analyze_with_retries(start: int, batch: list[dict]) -> list[Note]:
        for attempt in range(1, max_attempts + 1):
            try:
                return analyze_batch(client, deployment, batch)
            except Exception as exc:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"analysis batch starting at {start} failed after "
                        f"{max_attempts} attempts"
                    ) from exc
                print(
                    f"  [warn] batch starting at {start} failed "
                    f"(attempt {attempt}/{max_attempts}): {exc}; retrying"
                )
        raise AssertionError("unreachable")

    results: dict[int, list[Note]] = {}
    completed_comments = 0
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="analysis") as executor:
        futures = {
            executor.submit(analyze_with_retries, start, batch): (start, len(batch))
            for start, batch in batches
        }
        try:
            for future in as_completed(futures):
                start, batch_length = futures[future]
                results[start] = future.result()
                completed_comments += batch_length
                if progress_cb:
                    progress_cb(completed_comments, len(raw_comments))
        except Exception:
            for future in futures:
                future.cancel()
            raise

    # Restore input order regardless of which concurrent request completed first.
    return [note for start, _ in batches for note in results[start]]


def save_notes_json(notes: list[Note], path: str):
    with open(path, "w") as f:
        json.dump([asdict(n) for n in notes], f, indent=2)


def save_notes_markdown(notes: list[Note], path: str):
    by_repo: dict[str, list[Note]] = {}
    for n in notes:
        by_repo.setdefault(n.repo, []).append(n)

    lines = ["# Code Review Notes\n"]
    for repo, repo_notes in by_repo.items():
        lines.append(f"## {repo}\n")
        by_pr: dict[int, list[Note]] = {}
        for n in repo_notes:
            by_pr.setdefault(n.pr_number, []).append(n)
        for pr_number, pr_notes in by_pr.items():
            lines.append(f"### PR #{pr_number}: {pr_notes[0].pr_title}\n")
            for n in pr_notes:
                lines.append(f"- **[{n.category} / {n.severity}]** "
                             f"{n.file_path or '(general)'}\n"
                             f"  - Original: {n.original_issue}\n"
                             f"  - Requested: {n.requested_change}\n"
                             f"  - Why: {n.rationale}\n")
            lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
