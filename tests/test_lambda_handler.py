import base64
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import Mock, patch

import lambda_handler


def signed_event(body: bytes, *, base64_encoded: bool = False):
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return {
        "body": base64.b64encode(body).decode() if base64_encoded else body.decode(),
        "isBase64Encoded": base64_encoded,
        "headers": {
            "X-Hub-Signature-256": f"sha256={signature}",
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "pull_request",
        },
    }


class LambdaHandlerTests(unittest.TestCase):
    @patch("lambda_handler.configure_runtime_secrets")
    @patch("lambda_handler.build_job_publisher")
    def test_webhook_publishes_valid_job(self, build_publisher, configure_secrets):
        body = json.dumps({
            "action": "opened",
            "repository": {"full_name": "org/repo"},
            "pull_request": {"number": 12},
        }).encode()
        publisher = build_publisher.return_value

        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "secret"}):
            response = lambda_handler.webhook(signed_event(body), None)

        self.assertEqual(response["statusCode"], 202)
        publisher.publish.assert_called_once()
        self.assertEqual(publisher.publish.call_args.args[0]["work_type"], "analysis")

    @patch("lambda_handler.configure_runtime_secrets")
    @patch("lambda_handler.build_job_publisher")
    def test_webhook_verifies_decoded_base64_body(self, build_publisher, configure_secrets):
        body = json.dumps({
            "action": "opened",
            "repository": {"full_name": "org/repo"},
            "pull_request": {"number": 12},
        }).encode()

        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "secret"}):
            response = lambda_handler.webhook(
                signed_event(body, base64_encoded=True), None
            )

        self.assertEqual(response["statusCode"], 202)

    @patch("lambda_handler.build_event_processor")
    def test_worker_returns_only_failed_sqs_records(self, build_processor):
        processor = build_processor.return_value
        processor.process.side_effect = [None, RuntimeError("failed")]
        event = {"Records": [
            {"messageId": "one", "body": json.dumps({"version": 1})},
            {"messageId": "two", "body": json.dumps({"version": 1})},
        ]}

        response = lambda_handler.process_events(event, None)

        self.assertEqual(response, {"batchItemFailures": [{"itemIdentifier": "two"}]})


if __name__ == "__main__":
    unittest.main()