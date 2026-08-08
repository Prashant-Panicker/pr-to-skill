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
    }


def valid_item():
    return {
        "category": "error-handling",
        "original_issue": "The error was ignored.",
        "requested_change": "Handle the error.",
        "rationale": "Failures must be visible.",
        "severity": "blocking",
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def call_json(self, deployment, system_prompt, user_prompt):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class AnalyzeTests(unittest.TestCase):
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
