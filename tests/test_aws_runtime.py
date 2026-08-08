import os
import unittest
from unittest.mock import patch

import aws_runtime


class AwsRuntimeTests(unittest.TestCase):
    def tearDown(self):
        aws_runtime.load_secret.cache_clear()
        aws_runtime.build_job_publisher.cache_clear()
        aws_runtime.build_event_processor.cache_clear()

    @patch("aws_runtime.load_secret")
    def test_configure_runtime_secrets_maps_configured_secret_names(self, load_secret):
        load_secret.side_effect = lambda name: f"value-for-{name}"
        settings = {
            "GITHUB_WEBHOOK_SECRET_ID": "webhook-name",
            "GITHUB_APP_PRIVATE_KEY_SECRET_ID": "key-name",
            "AZURE_OPENAI_API_KEY_SECRET_ID": "azure-name",
        }
        with patch.dict(os.environ, settings, clear=True):
            aws_runtime.configure_runtime_secrets()
            self.assertEqual(os.environ["GITHUB_WEBHOOK_SECRET"],
                             "value-for-webhook-name")
            self.assertEqual(os.environ["GITHUB_APP_PRIVATE_KEY"],
                             "value-for-key-name")
            self.assertEqual(os.environ["AZURE_OPENAI_API_KEY"],
                             "value-for-azure-name")

    @patch("aws_runtime.boto3.client")
    def test_load_secret_rejects_binary_secret(self, boto_client):
        boto_client.return_value.get_secret_value.return_value = {
            "SecretBinary": b"not-supported"
        }

        with self.assertRaisesRegex(ValueError, "string value"):
            aws_runtime.load_secret("secret-name")


if __name__ == "__main__":
    unittest.main()