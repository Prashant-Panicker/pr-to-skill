"""One-repository webhook workflows for PR analysis and incremental learning."""

import json
import os
import time
from dataclasses import asdict

from artifact_store import LocalArtifactStore
import comment_analyzer
import github_collector
import skill_synthesizer


REVIEW_SYSTEM_PROMPT = """You are performing an advisory pull-request review using
only the supplied changed code and retrieved historical review evidence. Report
concrete findings with file names and rationale. Do not invent line numbers,
requirements, or business rules. If the evidence does not support a finding,
say that no repository-specific findings were identified. Return Markdown."""


def _comment_key(comment: dict) -> tuple[str, int | str]:
    return (
        comment["comment_type"],
        comment.get("github_comment_id") or comment.get("html_url", ""),
    )


class IncrementalPipeline:
    def __init__(
        self, client, deployment: str, store, config: dict,
        artifact_store=None, reconciliation_lock=None, deadline_seconds: int = 2700,
    ):
        self._client = client
        self._deployment = deployment
        self._store = store
        self._config = config
        self._output_dir = config["output"]["dir"]
        self._artifacts = artifact_store or LocalArtifactStore(self._output_dir)
        self._reconciliation_lock = reconciliation_lock
        self._deadline_seconds = deadline_seconds
        self._reviewer = config["person"]["github_username"]

    def analyze_pull_request(self, repo: str, pr_number: int) -> str:
        started_at = time.monotonic()
        pull_request = github_collector.get_pull_request(repo, pr_number)
        if self._reconciliation_lock is None:
            return self._analyze_pull_request(
                repo, pr_number, pull_request, None, started_at
            )
        lock_name = f"analysis-{repo.replace('/', '-')}-{pr_number}"
        with self._reconciliation_lock.lock(lock_name) as lease:
            current_pull_request = github_collector.get_pull_request(repo, pr_number)
            return self._analyze_pull_request(
                repo, pr_number, current_pull_request, lease, started_at
            )

    def _ensure_deadline(self, started_at: float) -> None:
        if time.monotonic() - started_at >= self._deadline_seconds:
            raise TimeoutError("Webhook workflow exceeded its processing deadline")

    def _analyze_pull_request(
        self, repo: str, pr_number: int, pull_request: dict, lease, started_at: float
    ) -> str:
        files = github_collector.get_pull_request_files(repo, pr_number)
        changed_code = "\n\n".join(
            f"File: {item.get('filename', '')}\n{item.get('patch', '')}"
            for item in files
        )[:60000]
        query = "\n".join((
            pull_request.get("title", ""),
            pull_request.get("body") or "",
            " ".join(item.get("filename", "") for item in files),
        ))
        evidence = self._store.search(
            query, repo, limit=8, reviewer=self._reviewer
        )
        prompt = (
            f"Repository: {repo}\nPull request: #{pr_number}\n"
            f"Title: {pull_request.get('title', '')}\n\n"
            f"<historical-evidence>\n{json.dumps(evidence, indent=2)}\n</historical-evidence>\n\n"
            f"<changed-code>\n{changed_code}\n</changed-code>"
        )
        review = self._client.call_text(
            self._deployment, REVIEW_SYSTEM_PROMPT, prompt, temperature=0.2
        )
        self._ensure_deadline(started_at)
        latest_pull_request = github_collector.get_pull_request(repo, pr_number)
        if latest_pull_request["head"]["sha"] != pull_request["head"]["sha"]:
            raise RuntimeError("Pull request head changed during analysis")
        if lease:
            lease.ensure_active()
        review_path = self._artifacts.write_text(f"reviews/pr-{pr_number}.md", review)
        return review_path

    def reconcile_feedback(self, repo: str, pr_number: int) -> bool:
        started_at = time.monotonic()
        if self._reconciliation_lock is None:
            return self._reconcile_feedback(repo, pr_number, started_at=started_at)
        with self._reconciliation_lock.lock("history-state") as lease:
            return self._reconcile_feedback(repo, pr_number, lease, started_at)

    def _reconcile_feedback(
        self, repo: str, pr_number: int, lease=None, started_at: float = 0
    ) -> bool:
        state = self._artifacts.read_json("pipeline_state.json")
        if not isinstance(state, dict) or state.get("version") != 1:
            raise RuntimeError(
                "Webhook history is not initialized; run --sync-azure-artifacts first"
            )
        existing_raw = state.get("raw_comments")
        existing_notes = state.get("notes")
        if not isinstance(existing_raw, list) or not isinstance(existing_notes, list):
            raise ValueError("pipeline_state.json contains invalid history arrays")
        current = [asdict(item) for item in github_collector.collect_for_pull_request(
            repo, self._reviewer, pr_number
        )]
        max_feedback = self._config.get("automation", {}).get(
            "max_feedback_per_pull_request", 100
        )
        if len(current) > max_feedback:
            raise ValueError(
                f"Pull request has {len(current)} review items; limit is {max_feedback}"
            )

        old_for_pr = [
            item for item in existing_raw
            if item["repo"] == repo and item["pr_number"] == pr_number
        ]
        if old_for_pr == current:
            return False

        current_keys = {_comment_key(item) for item in current}
        unchanged_notes = [
            note for note in existing_notes
            if not (note["repo"] == repo and note["pr_number"] == pr_number)
        ]
        prior_notes_for_pr = [
            note for note in existing_notes
            if note["repo"] == repo and note["pr_number"] == pr_number
        ]
        current_notes = comment_analyzer.analyze_all(
            self._client,
            self._deployment,
            current,
            batch_size=self._config.get("analysis", {}).get("batch_size", 8),
            max_attempts=self._config.get("analysis", {}).get("max_attempts", 3),
            max_workers=self._config.get("analysis", {}).get("workers", 4),
        ) if current else []
        notes = unchanged_notes + [asdict(note) for note in current_notes]
        raw_comments = [
            item for item in existing_raw
            if not (item["repo"] == repo and item["pr_number"] == pr_number)
        ] + current

        removed_notes = [
            note for note in prior_notes_for_pr if _comment_key(note) not in current_keys
        ]
        if lease:
            lease.ensure_active()
        self._store.delete_notes(removed_notes)
        if lease:
            lease.ensure_active()
        self._store.save_notes([asdict(note) for note in current_notes], self._reviewer)
        synthesis = self._config.get("synthesis", {})
        skill = skill_synthesizer.synthesize_skill(
            self._client, self._deployment, notes, self._reviewer,
            max_notes_per_call=synthesis.get("max_notes_per_call", 400),
            max_workers=synthesis.get("workers", 4),
        )
        self._ensure_deadline(started_at)
        if lease:
            lease.ensure_active()
        self._artifacts.write_text("raw_comments.json", json.dumps(raw_comments, indent=2))
        self._artifacts.write_text("notes.json", json.dumps(notes, indent=2))
        self._artifacts.write_text("SKILL.md", skill)
        self._artifacts.write_text(
            "pipeline_state.json",
            json.dumps({"version": 1, "raw_comments": raw_comments, "notes": notes}, indent=2),
        )
        return True