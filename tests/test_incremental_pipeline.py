import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from incremental_pipeline import IncrementalPipeline


def config(output_dir):
    return {
        "person": {"github_username": "reviewer"},
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
    @patch("incremental_pipeline.github_collector.collect_for_pull_request")
    def test_deleted_feedback_removes_vector_and_rebuilds_skill(self, collect, synthesize):
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
            store.save_notes.assert_called_once_with([], "reviewer")
            with open(os.path.join(output_dir, "notes.json")) as notes_file:
                self.assertEqual(json.load(notes_file), [])
            with open(os.path.join(output_dir, "SKILL.md")) as skill_file:
                self.assertIn("name: updated", skill_file.read())
            with open(os.path.join(output_dir, "pipeline_state.json")) as state_file:
                self.assertEqual(json.load(state_file)["notes"], [])

    @patch("incremental_pipeline.github_collector.collect_for_pull_request", return_value=[])
    def test_feedback_requires_initialized_state(self, collect):
        with tempfile.TemporaryDirectory() as output_dir:
            pipeline = IncrementalPipeline(Mock(), "model", Mock(), config(output_dir))

            with self.assertRaisesRegex(RuntimeError, "not initialized"):
                pipeline.reconcile_feedback("org/repo", 12)


if __name__ == "__main__":
    unittest.main()