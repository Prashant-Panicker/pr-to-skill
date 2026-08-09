"""Deterministic lifecycle rules applied after AI knowledge selection."""


def curate_notes(notes: list[dict]) -> list[dict]:
    selected = [
        note for note in notes
        if note.get("implemented", True)
        and note.get("include_in_vector_store", True)
    ]
    ordered = sorted(
        enumerate(selected),
        key=lambda item: (item[1].get("merged_at") or "", item[0]),
    )
    active: list[dict] = []
    for _, note in ordered:
        if note.get("supersedes_prior_architecture") and note.get("merged_at"):
            active = [
                existing for existing in active
                if not (
                    existing.get("repo") == note.get("repo")
                    and existing.get("category") == "architecture"
                    and existing.get("merged_at")
                    and existing["merged_at"] < note["merged_at"]
                )
            ]
        active.append(note)
    return active