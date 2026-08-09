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
review threads to understand their engineering standards and review philosophy. \
For each trusted reviewer's root comment, analyze the complete thread and the \
merged pull request's final diff. Replies may be from any author and can confirm, \
clarify, or reject the requested change. Stay strictly grounded in that evidence:

- category: exactly one of "api-design", "architecture", "concurrency", \
"documentation", "error-handling", "logging", "naming", "other", \
"performance", "readability", "security", "style", "testing"
- original_issue: what the code was doing before the comment (1 sentence)
- requested_change: what change is being asked for (1 sentence)
- rationale: why the reviewer wants this change, inferred from their wording \
(1 sentence)
- severity: one of "blocking", "suggestion", "nitpick"
- implemented: true only when the final merged diff implements the requested change
- include_in_vector_store: true only when this is durable, reusable review guidance \
with a useful concrete example; this must be false when implemented is false
- selection_rationale: one sentence explaining the inclusion decision
- implementation_example: a concise example grounded in the final merged diff; \
use an empty string when include_in_vector_store is false
- architecture_change: true when the accepted change establishes an architecture
- supersedes_prior_architecture: true only when the accepted architecture explicitly \
replaces the repository's previous architecture

Return a JSON object of the form {"items": [{...}, {...}]}, one item per input \
comment, in the same order as given. Do not include any text outside the JSON."""

ARCHITECTURE_SYSTEM_PROMPT = """Assess whether a merged pull request establishes \
a repository architecture decision. Use only the PR title, description, and final \
merged diff. Return JSON with: architecture_change (boolean), implemented (boolean), \
supersedes_prior_architecture (boolean), include_in_vector_store (boolean), summary \
(non-empty sentence), and rationale (non-empty sentence). Set supersedes true only \
when the PR explicitly replaces an older architecture. Set include true only for an \
implemented, durable architecture decision useful in future reviews."""

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
    "implemented",
    "include_in_vector_store",
    "selection_rationale",
    "implementation_example",
    "architecture_change",
    "supersedes_prior_architecture",
}
BOOLEAN_ITEM_FIELDS = {
    "implemented",
    "include_in_vector_store",
    "architecture_change",
    "supersedes_prior_architecture",
}


@dataclass
class Note:
    repo: str
    pr_number: int
    github_comment_id: int
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
    reviewer: str
    implemented: bool
    include_in_vector_store: bool
    selection_rationale: str
    implementation_example: str
    architecture_change: bool
    supersedes_prior_architecture: bool
    merged_at: str


def _format_comment_for_prompt(c: dict, idx: int) -> str:
    lines = [f"--- Comment {idx} ---"]
    lines.append(f"Repo: {c['repo']}  PR #{c['pr_number']}: {c['pr_title']}")
    lines.append(f"Type: {c['comment_type']}" + (f"  (review_state: {c['review_state']})" if c.get("review_state") else ""))
    if c.get("file_path"):
        lines.append(f"File: {c['file_path']}")
    if c.get("diff_hunk"):
        lines.append(f"Diff hunk:\n{c['diff_hunk']}")
    lines.append(f"Trusted reviewer: {c.get('reviewer', '')}")
    lines.append(f"Root comment: {c['body']}")
    for reply in c.get("replies", []):
        lines.append(f"Reply by {reply.get('author', '')}: {reply.get('body', '')}")
    return "\n".join(lines)


def analyze_batch(client, deployment: str, batch: list[dict]) -> list[Note]:
    prompt_parts = [_format_comment_for_prompt(c, i) for i, c in enumerate(batch)]
    pull_request_diffs = {}
    for comment in batch:
        key = (comment["repo"], comment["pr_number"])
        pull_request_diffs.setdefault(key, comment.get("final_diff", ""))
    prompt_parts.extend(
        f"--- Final merged diff for {repo} PR #{pr_number} ---\n{final_diff}"
        for (repo, pr_number), final_diff in pull_request_diffs.items()
    )
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
        for field_name in REQUIRED_ITEM_FIELDS - BOOLEAN_ITEM_FIELDS - {"implementation_example"}:
            if not isinstance(item[field_name], str) or not item[field_name].strip():
                raise ValueError(
                    f"analysis item {index} field '{field_name}' must be a non-empty string"
                )
        for field_name in BOOLEAN_ITEM_FIELDS:
            if not isinstance(item[field_name], bool):
                raise ValueError(
                    f"analysis item {index} field '{field_name}' must be a boolean"
                )
        if item["include_in_vector_store"] and not item["implemented"]:
            raise ValueError(
                f"analysis item {index} cannot include unimplemented guidance"
            )
        if not isinstance(item["implementation_example"], str):
            raise ValueError(
                f"analysis item {index} field 'implementation_example' must be a string"
            )
        if item["include_in_vector_store"] and not item["implementation_example"].strip():
            raise ValueError(
                f"analysis item {index} selected guidance requires an implementation example"
            )
        if item["supersedes_prior_architecture"] and not item["architecture_change"]:
            raise ValueError(
                f"analysis item {index} cannot supersede a non-architecture change"
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
            repo=c["repo"], pr_number=c["pr_number"],
            github_comment_id=c["github_comment_id"], pr_title=c["pr_title"],
            pr_url=c["pr_url"], comment_type=c["comment_type"], file_path=c.get("file_path"),
            original_body=c["body"],
            category=item["category"],
            original_issue=item["original_issue"],
            requested_change=item["requested_change"],
            rationale=item["rationale"],
            severity=item["severity"],
            reviewer=c.get("reviewer", ""),
            implemented=item["implemented"],
            include_in_vector_store=item["include_in_vector_store"],
            selection_rationale=item["selection_rationale"],
            implementation_example=item["implementation_example"],
            architecture_change=item["architecture_change"],
            supersedes_prior_architecture=item["supersedes_prior_architecture"],
            merged_at=c.get("merged_at", ""),
        ))
    return notes


def analyze_architecture_change(
    client, deployment: str, pull_request: dict, final_diff: str,
    repo: str | None = None,
) -> Note | None:
    repository = repo or pull_request.get("base", {}).get("repo", {}).get("full_name")
    if not repository:
        raise ValueError("Architecture assessment requires a repository")
    prompt = (
        f"Repository: {repository}\n"
        f"PR #{pull_request['number']}: {pull_request.get('title', '')}\n"
        f"Description:\n{pull_request.get('body') or ''}\n\n"
        f"Final merged diff:\n{final_diff}"
    )
    item = client.call_json(deployment, ARCHITECTURE_SYSTEM_PROMPT, prompt)
    boolean_fields = {
        "architecture_change", "implemented", "supersedes_prior_architecture",
        "include_in_vector_store",
    }
    for field_name in boolean_fields:
        if not isinstance(item.get(field_name), bool):
            raise ValueError(f"architecture assessment field '{field_name}' must be boolean")
    for field_name in ("summary", "rationale"):
        if not isinstance(item.get(field_name), str) or not item[field_name].strip():
            raise ValueError(f"architecture assessment field '{field_name}' must be non-empty")
    if item["supersedes_prior_architecture"] and not item["architecture_change"]:
        raise ValueError("architecture assessment cannot supersede a non-architecture change")
    if item["include_in_vector_store"] and not (
        item["architecture_change"] and item["implemented"]
    ):
        raise ValueError("architecture assessment cannot include an unimplemented decision")
    if not item["include_in_vector_store"]:
        return None
    pr_number = pull_request["number"]
    return Note(
        repo=repository, pr_number=pr_number, github_comment_id=-pr_number,
        pr_title=pull_request.get("title", ""),
        pr_url=pull_request.get("html_url", ""),
        comment_type="pull_request_architecture", file_path=None,
        original_body=pull_request.get("body") or item["summary"],
        category="architecture", original_issue=item["summary"],
        requested_change=item["summary"], rationale=item["rationale"],
        severity="blocking", reviewer="__merged_pr__", implemented=True,
        include_in_vector_store=True,
        selection_rationale="Implemented merged-PR architecture decision.",
        implementation_example=item["summary"],
        architecture_change=True,
        supersedes_prior_architecture=item["supersedes_prior_architecture"],
        merged_at=pull_request.get("merged_at", ""),
    )


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


def analyze_history(
    client,
    deployment: str,
    raw_comments: list[dict],
    merged_pull_requests: list[dict],
    **analysis_options,
) -> list[Note]:
    notes = analyze_all(
        client, deployment, raw_comments, **analysis_options
    ) if raw_comments else []
    for evidence in merged_pull_requests:
        architecture_note = analyze_architecture_change(
            client,
            deployment,
            evidence["pull_request"],
            evidence["final_diff"],
        )
        if architecture_note:
            notes.append(architecture_note)
    return notes


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
