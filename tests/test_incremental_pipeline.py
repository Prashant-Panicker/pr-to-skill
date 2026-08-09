import json
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import comment_analyzer
import github_collector
from incremental_pipeline import IncrementalPipeline


def config(output_dir):
    return {
        "person": {"github_usernames": ["reviewer", "architect"]},
        "output": {"dir": output_dir},
        "analysis": {"batch_size": 2, "max_attempts": 1, "workers": 1},
        "synthesis": {"max_notes_per_call": 10, "workers": 1},
        "automation": {"post_review_comment": False},
    }


def old_comment():
    return {
        "repo": "org/repo", "pr_number": 12, "github_comment_id": 99,
        "comment_type": "review_comment", "html_url": "https://example/99",
    }


def old_note():
    return {
        **old_comment(),
        "pr_title": "PR", "pr_url": "https://example/pr/12", "file_path": "api.py",
        "original_body": "Validate this.", "category": "security",
        "original_issue": "Input was trusted.", "requested_change": "Validate it.",
        "rationale": "Input is untrusted.", "severity": "blocking",
    }


def raw_feedback(comment_id=100, body="Use the new architecture."):
    return github_collector.RawComment(
        repo="org/repo", pr_number=12, github_comment_id=comment_id,
        pr_title="Architecture", pr_url="https://example/pr/12", pr_state="merged",
        comment_type="review_comment", file_path="service.py", diff_hunk="@@ old @@",
        body=body, review_state=None, created_at="2026-01-01",
        html_url=f"https://example/{comment_id}", reviewer="architect", replies=[],
        final_diff="+class NewArchitecture:", merged_at="2026-01-02",
    )


def analyzed_note(**overrides):
    values = {
        "repo": "org/repo", "pr_number": 12, "github_comment_id": 100,
        "pr_title": "Architecture", "pr_url": "https://example/pr/12",
        "comment_type": "review_comment", "file_path": "service.py",
        "original_body": "Use the new architecture.", "category": "architecture",
        "original_issue": "The old architecture was used.",
        "requested_change": "Use the new architecture.",
        "rationale": "The new boundary is now canonical.", "severity": "blocking",
        "reviewer": "architect", "implemented": True,
        "include_in_vector_store": True,
        "selection_rationale": "Implemented reusable architecture guidance.",
        "implementation_example": "Infrastructure is accessed through a port.",
        "architecture_change": True, "supersedes_prior_architecture": True,
        "merged_at": "2026-01-02T00:00:00Z",
    }
    values.update(overrides)
    return comment_analyzer.Note(**values)


class IncrementalPipelineTests(unittest.TestCase):
    @patch("incremental_pipeline.github_collector.get_pull_request_files")
    @patch("incremental_pipeline.github_collector.get_pull_request")
    def test_opened_pr_retrieves_evidence_and_writes_review(self, get_pr, get_files):
        get_pr.return_value = {
            "title": "Validate input", "body": "API change",
            "head": {"sha": "a" * 40},
        }
        get_files.return_value = [{"filename": "api.py", "patch": "+value = request.body"}]
        client = Mock()
        client.call_text.return_value = "# Review\n\nValidate the request body."
        store = Mock()
        store.search.return_value = [{"content": "Validate untrusted input."}]

        with tempfile.TemporaryDirectory() as output_dir:
            pipeline = IncrementalPipeline(client, "model", store, config(output_dir))
            review_path = pipeline.analyze_pull_request("org/repo", 12)

            self.assertTrue(os.path.exists(review_path))
            with open(review_path) as review_file:
                self.assertIn("Validate the request body", review_file.read())
        store.search.assert_called_once()
        self.assertIn("historical-evidence", client.call_text.call_args.args[2])

    @patch("incremental_pipeline.github_collector.get_pull_request_files")
    @patch("incremental_pipeline.github_collector.get_pull_request")
    def test_opened_pr_publishes_review_against_analyzed_head(
        self, get_pr, get_files
    ):
        get_pr.return_value = {
            "title": "PR", "body": "", "head": {"sha": "a" * 40}
        }
        get_files.return_value = [{"filename": "api.py", "patch": "+change"}]
        client = Mock()
        client.call_text.return_value = "# Review"
        store = Mock()
        store.search.return_value = []
        publisher = Mock()

        with tempfile.TemporaryDirectory() as output_dir:
            pipeline = IncrementalPipeline(
                client, "model", store, config(output_dir),
                review_publisher=publisher,
            )
            pipeline.analyze_pull_request("org/repo", 12)

        publisher.publish.assert_called_once_with(
            "org/repo", 12, "# Review", "a" * 40
        )

    @patch("incremental_pipeline.github_collector.get_pull_request_files")
    @patch("incremental_pipeline.github_collector.get_pull_request")
    def test_does_not_publish_review_when_head_changes(self, get_pr, get_files):
        get_pr.side_effect = [
            {"title": "PR", "body": "", "head": {"sha": "a" * 40}},
            {"title": "PR", "body": "", "head": {"sha": "b" * 40}},
        ]
        get_files.return_value = [{"filename": "api.py", "patch": "+change"}]
        client = Mock()
        client.call_text.return_value = "# Review"
        store = Mock()
        store.search.return_value = []

        with tempfile.TemporaryDirectory() as output_dir:
            pipeline = IncrementalPipeline(client, "model", store, config(output_dir))

            with self.assertRaisesRegex(RuntimeError, "head changed"):
                pipeline.analyze_pull_request("org/repo", 12)

            self.assertFalse(os.path.exists(
                os.path.join(output_dir, "reviews", "pr-12.md")
            ))

    @patch("incremental_pipeline.skill_synthesizer.synthesize_skill")
    @patch("incremental_pipeline.comment_analyzer.analyze_architecture_change", return_value=None)
    @patch("incremental_pipeline.github_collector.get_pull_request_diff", return_value="diff")
    @patch("incremental_pipeline.github_collector.collect_for_pull_request")
    @patch("incremental_pipeline.github_collector.get_pull_request")
    def test_deleted_feedback_removes_vector_and_rebuilds_skill(
        self, get_pr, collect, get_diff, analyze_architecture, synthesize
    ):
        get_pr.return_value = {"state": "closed", "merged_at": "2026-01-01"}
        collect.return_value = []
        synthesize.return_value = "---\nname: updated\n---\n"
        store = Mock()

        with tempfile.TemporaryDirectory() as output_dir:
            with open(os.path.join(output_dir, "pipeline_state.json"), "w") as output:
                json.dump({
                    "version": 1,
                    "raw_comments": [old_comment()],
                    "notes": [old_note()],
                }, output)
            pipeline = IncrementalPipeline(Mock(), "model", store, config(output_dir))

            changed = pipeline.reconcile_feedback("org/repo", 12)

            self.assertTrue(changed)
            store.delete_notes.assert_called_once_with([old_note()])
            store.save_notes.assert_called_once_with([])
            with open(os.path.join(output_dir, "notes.json")) as notes_file:
                self.assertEqual(json.load(notes_file), [])
            with open(os.path.join(output_dir, "SKILL.md")) as skill_file:
                self.assertIn("name: updated", skill_file.read())
            with open(os.path.join(output_dir, "pipeline_state.json")) as state_file:
                state = json.load(state_file)
                self.assertEqual(state["version"], 2)
                self.assertEqual(state["notes"], [])

    @patch("incremental_pipeline.github_collector.get_pull_request")
    def test_open_pr_is_remembered_without_mining(self, get_pr):
        get_pr.return_value = {"state": "open", "merged_at": None}
        with tempfile.TemporaryDirectory() as output_dir:
            with open(os.path.join(output_dir, "pipeline_state.json"), "w") as output:
                json.dump({"version": 2, "raw_comments": [], "notes": [],
                           "pending_prs": []}, output)
            pipeline = IncrementalPipeline(Mock(), "model", Mock(), config(output_dir))

            self.assertTrue(pipeline.mine_pull_request("org/repo", 12))

            with open(os.path.join(output_dir, "pipeline_state.json")) as state_file:
                self.assertEqual(json.load(state_file)["pending_prs"], [
                    {"repo": "org/repo", "pr_number": 12}
                ])

    @patch("incremental_pipeline.skill_synthesizer.synthesize_skill", return_value="# Skill")
    @patch("incremental_pipeline.comment_analyzer.analyze_architecture_change", return_value=None)
    @patch("incremental_pipeline.comment_analyzer.analyze_all")
    @patch("incremental_pipeline.github_collector.collect_for_pull_request")
    @patch("incremental_pipeline.github_collector.get_pull_request")
    def test_ai_exclusion_removes_previously_indexed_note(
        self, get_pr, collect, analyze, analyze_architecture, synthesize
    ):
        get_pr.return_value = {"state": "closed", "merged_at": "2026-01-02"}
        collect.return_value = [raw_feedback()]
        analyze.return_value = [replace(
            analyzed_note(), include_in_vector_store=False,
            selection_rationale="Too specific to reuse.",
            architecture_change=False, supersedes_prior_architecture=False,
        )]
        prior = {**old_note(), "github_comment_id": 100, "reviewer": "architect"}
        store = Mock()

        with tempfile.TemporaryDirectory() as output_dir:
            with open(os.path.join(output_dir, "pipeline_state.json"), "w") as output:
                json.dump({"version": 2, "raw_comments": [], "notes": [prior],
                           "pending_prs": []}, output)
            pipeline = IncrementalPipeline(Mock(), "model", store, config(output_dir))

            pipeline.mine_pull_request("org/repo", 12)

        store.delete_notes.assert_called_once_with([prior])
        store.save_notes.assert_called_once_with([])

    @patch("incremental_pipeline.skill_synthesizer.synthesize_skill", return_value="# Skill")
    @patch("incremental_pipeline.comment_analyzer.analyze_architecture_change", return_value=None)
    @patch("incremental_pipeline.comment_analyzer.analyze_all")
    @patch("incremental_pipeline.github_collector.collect_for_pull_request")
    @patch("incremental_pipeline.github_collector.get_pull_request")
    def test_merged_architecture_supersedes_only_older_architecture_notes(
        self, get_pr, collect, analyze, analyze_architecture, synthesize
    ):
        get_pr.return_value = {"state": "closed", "merged_at": "2026-01-02"}
        collect.return_value = [raw_feedback()]
        analyze.return_value = [analyzed_note()]
        old_architecture = {
            **old_note(), "pr_number": 7, "category": "architecture",
            "merged_at": "2026-01-01T00:00:00Z",
        }
        old_security = {
            **old_note(), "pr_number": 8, "github_comment_id": 98,
            "category": "security",
        }
        store = Mock()

        with tempfile.TemporaryDirectory() as output_dir:
            with open(os.path.join(output_dir, "pipeline_state.json"), "w") as output:
                json.dump({"version": 2, "raw_comments": [],
                           "notes": [old_architecture, old_security],
                           "pending_prs": []}, output)
            pipeline = IncrementalPipeline(Mock(), "model", store, config(output_dir))

            pipeline.mine_pull_request("org/repo", 12)

            with open(os.path.join(output_dir, "notes.json")) as notes_file:
                notes = json.load(notes_file)

        store.delete_notes.assert_called_once_with([old_architecture])
        self.assertIn(old_security, notes)
        self.assertNotIn(old_architecture, notes)

    @patch("incremental_pipeline.github_collector.collect_for_pull_request", return_value=[])
    def test_feedback_requires_initialized_state(self, collect):
        with tempfile.TemporaryDirectory() as output_dir:
            pipeline = IncrementalPipeline(Mock(), "model", Mock(), config(output_dir))

            with self.assertRaisesRegex(RuntimeError, "not initialized"):
                pipeline.reconcile_feedback("org/repo", 12)


if __name__ == "__main__":
    unittest.main()