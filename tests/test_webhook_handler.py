import hashlib
import hmac
import json
import unittest

import webhook_handler


class WebhookHandlerTests(unittest.TestCase):
    def test_verifies_signature_over_exact_raw_body(self):
        body = b'{"action":"opened"}'
        digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()

        self.assertTrue(webhook_handler.verify_signature(
            "secret", body, f"sha256={digest}"
        ))
        self.assertFalse(webhook_handler.verify_signature(
            "secret", body + b" ", f"sha256={digest}"
        ))

    def test_routes_opened_pull_request_to_review(self):
        body = json.dumps({
            "action": "opened",
            "number": 12,
            "repository": {"full_name": "org/repo"},
            "pull_request": {"number": 12},
        }).encode()

        result = webhook_handler.route_delivery("delivery-1", "pull_request", body)

        self.assertEqual(result["work_type"], "review")
        self.assertEqual(result["repo"], "org/repo")
        self.assertEqual(result["pr_number"], 12)

    def test_routes_review_comment_edit_to_mining(self):
        body = json.dumps({
            "action": "edited",
            "repository": {"full_name": "org/repo"},
            "pull_request": {"number": 12},
        }).encode()

        result = webhook_handler.route_delivery(
            "delivery-2", "pull_request_review_comment", body
        )

        self.assertEqual(result["work_type"], "mining")

    def test_routes_closed_pull_request_to_mining(self):
        body = json.dumps({
            "action": "closed",
            "repository": {"full_name": "org/repo"},
            "pull_request": {"number": 12},
        }).encode()

        result = webhook_handler.route_delivery("delivery-3", "pull_request", body)

        self.assertEqual(result["work_type"], "mining")

    def test_routes_pr_issue_comment_to_mining(self):
        body = json.dumps({
            "action": "created",
            "repository": {"full_name": "org/repo"},
            "issue": {"number": 12, "pull_request": {"url": "https://api/pulls/12"}},
        }).encode()

        result = webhook_handler.route_delivery("delivery-4", "issue_comment", body)

        self.assertEqual(result["work_type"], "mining")
        self.assertEqual(result["pr_number"], 12)

    def test_ignores_non_pr_issue_comment(self):
        body = json.dumps({
            "action": "created",
            "repository": {"full_name": "org/repo"},
            "issue": {"number": 12},
        }).encode()

        self.assertIsNone(
            webhook_handler.route_delivery("delivery-5", "issue_comment", body)
        )

    def test_ignores_unsupported_events(self):
        body = json.dumps({"action": "labeled"}).encode()

        self.assertIsNone(webhook_handler.route_delivery(
            "delivery-3", "pull_request", body
        ))


if __name__ == "__main__":
    unittest.main()