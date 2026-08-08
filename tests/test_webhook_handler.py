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

    def test_routes_opened_pull_request_to_analysis(self):
        body = json.dumps({
            "action": "opened",
            "number": 12,
            "repository": {"full_name": "org/repo"},
            "pull_request": {"number": 12},
        }).encode()

        result = webhook_handler.route_delivery("delivery-1", "pull_request", body)

        self.assertEqual(result["work_type"], "analysis")
        self.assertEqual(result["repo"], "org/repo")
        self.assertEqual(result["pr_number"], 12)

    def test_routes_review_comment_edit_to_history(self):
        body = json.dumps({
            "action": "edited",
            "repository": {"full_name": "org/repo"},
            "pull_request": {"number": 12},
        }).encode()

        result = webhook_handler.route_delivery(
            "delivery-2", "pull_request_review_comment", body
        )

        self.assertEqual(result["work_type"], "history")

    def test_ignores_unsupported_events(self):
        body = json.dumps({"action": "labeled"}).encode()

        self.assertIsNone(webhook_handler.route_delivery(
            "delivery-3", "pull_request", body
        ))


if __name__ == "__main__":
    unittest.main()