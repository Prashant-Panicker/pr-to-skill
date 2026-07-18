"""
Stage 2 of the AI pipeline: roll up every structured note into a single
SKILL.md, following the same banded-checklist format used elsewhere
(e.g. the React PR review skill): a short frontmatter description, then
grouped, prioritized checklist items with rationale and a short example
where useful.
"""

import json
from concurrent.futures import ThreadPoolExecutor

SYSTEM_PROMPT = """You are building a reusable code-review "skill" document that \
encodes one specific engineer's review standards, learned from hundreds of their \
past PR review comments (each pre-tagged with category, what was wrong, what they \
asked for, why, and severity).

Produce a Markdown skill file with this structure:

1. YAML frontmatter with `name` and a one-sentence `description` (written so an \
AI coding assistant knows when to load this skill — e.g. "Use when reviewing pull \
requests to apply <person>'s established engineering standards").
2. A short intro paragraph summarizing this person's overall review philosophy \
in 2-3 sentences (e.g. what they consistently prioritize).
3. Checklist items grouped into bands by category (e.g. "Error Handling & \
Resilience", "Naming & Readability", "Security", "Testing", "Architecture & \
API Design", "Performance", etc.) — only include bands that actually have \
supporting evidence in the notes, don't invent categories with no data.
4. Within each band, list concrete, actionable checklist items. Each item should \
be phrased as an instruction ("Ensure X", "Avoid Y", "Prefer Z"), followed by a \
one-line rationale in parentheses. Merge near-duplicate notes into a single \
well-phrased item rather than listing every instance.
5. Mark items that came from "blocking"-severity comments as **(blocking)**.
6. Do not fabricate examples or claims not supported by the provided notes.

Return only the Markdown content, no extra commentary."""


def build_synthesis_prompt(notes: list[dict], person_username: str) -> str:
    # Compact representation to keep token usage reasonable across hundreds of notes
    compact = [
        {
            "category": n["category"],
            "severity": n["severity"],
            "original_issue": n["original_issue"],
            "requested_change": n["requested_change"],
            "rationale": n["rationale"],
        }
        for n in notes
    ]
    return (
        f"Engineer GitHub username: {person_username}\n"
        f"Total review notes: {len(compact)}\n\n"
        f"Notes (JSON array):\n{json.dumps(compact, indent=1)}"
    )


def synthesize_skill(client, deployment: str, notes: list[dict], person_username: str,
                     max_notes_per_call: int = 400, max_workers: int = 4) -> str:
    """
    For very large note sets, this does a single pass if under max_notes_per_call,
    otherwise chunks and does a two-pass reduce (summarize chunks, then merge).
    """
    if max_notes_per_call < 1:
        raise ValueError("max_notes_per_call must be at least 1")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    if len(notes) <= max_notes_per_call:
        prompt = build_synthesis_prompt(notes, person_username)
        return client.call_text(
            deployment,
            SYSTEM_PROMPT,
            prompt,
            temperature=0.3,
        )

    # Map step: summarize each chunk into a condensed set of patterns
    chunks = [
        notes[i:i + max_notes_per_call]
        for i in range(0, len(notes), max_notes_per_call)
    ]

    def summarize_chunk(chunk: list[dict]) -> str:
        prompt = build_synthesis_prompt(chunk, person_username)
        return client.call_text(
            deployment,
            SYSTEM_PROMPT + "\n\nThis is a partial chunk of a larger note set — "
            "produce condensed bullet patterns per category rather than a full skill "
            "file; a later pass will merge chunks.",
            prompt,
            temperature=0.3,
        )

    # map preserves chunk order while Azure calls execute concurrently.
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="synthesis") as executor:
        chunk_summaries = list(executor.map(summarize_chunk, chunks))

    # Reduce step: merge chunk summaries into the final skill file
    merge_prompt = (
        f"Engineer GitHub username: {person_username}\n\n"
        "Below are condensed pattern summaries from multiple chunks of this "
        "engineer's review history. Merge them into one final skill file, "
        "deduplicating overlapping items and following the structure described "
        "in your instructions.\n\n" + "\n\n---\n\n".join(chunk_summaries)
    )
    return client.call_text(
        deployment,
        SYSTEM_PROMPT,
        merge_prompt,
        temperature=0.3,
    )


def save_skill(skill_markdown: str, path: str):
    with open(path, "w") as f:
        f.write(skill_markdown)
