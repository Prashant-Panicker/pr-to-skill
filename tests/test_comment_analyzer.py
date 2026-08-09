import unittest
import threading

import comment_analyzer


def raw_comment(number=1):
    return {
        "repo": "org/repo",
        "pr_number": number,
        "github_comment_id": 1000 + number,
        "pr_title": "A pull request",
        "pr_url": f"https://example.test/pull/{number}",
        "comment_type": "review_comment",
        "review_state": None,
        "file_path": "example.py",
        "diff_hunk": "@@ example @@",
        "body": "Please handle this error.",
        "reviewer": "reviewer",
        "replies": [{"author": "author", "body": "Fixed in the next commit."}],
        "final_diff": "+raise ApplicationError()",
        "merged_at": "2026-01-03T00:00:00Z",
    }


def valid_item():
    return {
        "category": "error-handling",
        "original_issue": "The error was ignored.",
        "requested_change": "Handle the error.",
        "rationale": "Failures must be visible.",
        "severity": "blocking",
        "implemented": True,
        "include_in_vector_store": True,
        "selection_rationale": "This is reusable and implemented guidance.",
        "implementation_example": "Raise ApplicationError instead of ignoring it.",
        "architecture_change": False,
        "supersedes_prior_architecture": False,
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.prompts = []

    def call_json(self, deployment, system_prompt, user_prompt):
        self.calls += 1
        self.prompts.append(user_prompt)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class AnalyzeTests(unittest.TestCase):
    def test_history_analyzes_architecture_pr_without_trusted_comments(self):
        client = FakeClient([{
            "architecture_change": True,
            "implemented": True,
            "supersedes_prior_architecture": False,
            "include_in_vector_store": True,
            "summary": "Introduce application-owned ports.",
            "rationale": "Infrastructure is now replaceable.",
        }])
        pull_request = {
            "number": 12, "title": "Introduce ports", "body": "Decouple AWS.",
            "html_url": "https://example/pull/12",
            "base": {"repo": {"full_name": "org/repo"}},
            "merged_at": "2026-01-03T00:00:00Z",
        }

        notes = comment_analyzer.analyze_history(
            client, "deployment", [],
            [{"pull_request": pull_request, "final_diff": "+class Port:"}],
        )

        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].comment_type, "pull_request_architecture")

    def test_pr_description_can_produce_architecture_note(self):
        client = FakeClient([{
            "architecture_change": True,
            "implemented": True,
            "supersedes_prior_architecture": True,
            "include_in_vector_store": True,
            "summary": "Use ports instead of direct infrastructure dependencies.",
            "rationale": "Ports are now the repository architecture.",
        }])
        pull_request = {
            "number": 12, "title": "Replace service architecture",
            "body": "Move infrastructure behind ports.",
            "html_url": "https://example/pull/12",
            "base": {"repo": {"full_name": "org/repo"}},
            "merged_at": "2026-01-03T00:00:00Z",
        }

        note = comment_analyzer.analyze_architecture_change(
            client, "deployment", pull_request, "+class Port:"
        )

        self.assertEqual(note.reviewer, "__merged_pr__")
        self.assertTrue(note.supersedes_prior_architecture)
        self.assertEqual(note.github_comment_id, -12)

    def test_pr_architecture_rejects_unimplemented_selection(self):
        client = FakeClient([{
            "architecture_change": True,
            "implemented": False,
            "supersedes_prior_architecture": False,
            "include_in_vector_store": True,
            "summary": "Proposed architecture.",
            "rationale": "It was discussed but not implemented.",
        }])
        pull_request = {
            "number": 12, "title": "Architecture proposal", "body": "Proposal",
            "html_url": "https://example/pull/12",
            "base": {"repo": {"full_name": "org/repo"}},
            "merged_at": "2026-01-03T00:00:00Z",
        }

        with self.assertRaisesRegex(ValueError, "unimplemented decision"):
            comment_analyzer.analyze_architecture_change(
                client, "deployment", pull_request, "+unrelated"
            )

    def test_runs_batches_concurrently_but_preserves_comment_order(self):
        rendezvous = threading.Barrier(2)

        class ConcurrentClient:
            def call_json(self, deployment, system_prompt, user_prompt):
                rendezvous.wait(timeout=1)
                item = valid_item()
                item["original_issue"] = (
                    "first" if "PR #1" in user_prompt else "second"
                )
                return {"items": [item]}

        notes = comment_analyzer.analyze_all(
            ConcurrentClient(),
            "deployment",
            [raw_comment(1), raw_comment(2)],
            batch_size=1,
            max_workers=2,
        )

        self.assertEqual([note.pr_number for note in notes], [1, 2])
        self.assertEqual([note.original_issue for note in notes], ["first", "second"])

    def test_rejects_a_short_response_instead_of_truncating(self):
        client = FakeClient([{"items": [valid_item()]}])

        with self.assertRaisesRegex(ValueError, "1 items for 2 comments"):
            comment_analyzer.analyze_batch(
                client, "deployment", [raw_comment(1), raw_comment(2)]
            )

    def test_rejects_invalid_enums(self):
        item = valid_item()
        item["severity"] = "critical"

        with self.assertRaisesRegex(ValueError, "invalid severity"):
            comment_analyzer.analyze_batch(
                FakeClient([{"items": [item]}]), "deployment", [raw_comment()]
            )

    def test_rejects_unimplemented_vector_selection(self):
        item = valid_item()
        item["implemented"] = False

        with self.assertRaisesRegex(ValueError, "unimplemented guidance"):
            comment_analyzer.analyze_batch(
                FakeClient([{"items": [item]}]), "deployment", [raw_comment()]
            )

    def test_prompt_contains_replies_and_final_merged_diff(self):
        client = FakeClient([{"items": [valid_item()]}])

        comment_analyzer.analyze_batch(client, "deployment", [raw_comment()])

        self.assertIn("Reply by author: Fixed in the next commit.", client.prompts[0])
        self.assertIn(
            "Final merged diff for org/repo PR #1 ---\n+raise ApplicationError()",
            client.prompts[0],
        )

    def test_retries_then_returns_complete_batch(self):
        client = FakeClient(
            [
                {"items": []},
                {"items": [valid_item()]},
            ]
        )

        notes = comment_analyzer.analyze_all(
            client, "deployment", [raw_comment()], max_attempts=2
        )

        self.assertEqual(len(notes), 1)
        self.assertEqual(client.calls, 2)

    def test_raises_after_retries_are_exhausted(self):
        client = FakeClient([{"items": []}, {"items": []}])

        with self.assertRaisesRegex(RuntimeError, "failed after 2 attempts"):
            comment_analyzer.analyze_all(
                client, "deployment", [raw_comment()], max_attempts=2
            )


if __name__ == "__main__":
    unittest.main()
