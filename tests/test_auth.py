import importlib
import sys
import types
import unittest
from unittest.mock import Mock


identity_stub = types.ModuleType("azure.identity")
identity_stub.DefaultAzureCredential = Mock(return_value="credential")
identity_stub.get_bearer_token_provider = Mock(return_value="provider")
azure_stub = types.ModuleType("azure")
azure_stub.identity = identity_stub
sys.modules["azure"] = azure_stub
sys.modules["azure.identity"] = identity_stub

sys.modules.pop("auth", None)
auth = importlib.import_module("auth")


class AuthTests(unittest.TestCase):
    def setUp(self):
        identity_stub.DefaultAzureCredential.reset_mock()
        identity_stub.get_bearer_token_provider.reset_mock()

    def test_matches_azure_code_agent_credential_chain(self):
        provider = auth.get_token_provider()

        self.assertEqual(provider, "provider")
        identity_stub.DefaultAzureCredential.assert_called_once_with(
            exclude_environment_credential=False,
            exclude_managed_identity_credential=True,
            exclude_shared_token_cache_credential=False,
            exclude_visual_studio_code_credential=True,
            exclude_cli_credential=False,
        )
        identity_stub.get_bearer_token_provider.assert_called_once_with(
            "credential", "https://cognitiveservices.azure.com/.default"
        )


class ApiKeyTests(unittest.TestCase):
    def test_environment_key_takes_precedence_over_config(self):
        self.assertEqual(
            auth.get_api_key({"api_key": "config-key"}, {"AZURE_OPENAI_API_KEY": "env-key"}),
            "env-key",
        )

    def test_falls_back_to_config_key(self):
        self.assertEqual(auth.get_api_key({"api_key": "config-key"}, {}), "config-key")

    def test_returns_none_when_no_key_configured(self):
        self.assertIsNone(auth.get_api_key({}, {}))

    def test_blank_key_is_treated_as_absent(self):
        self.assertIsNone(auth.get_api_key({"api_key": "   "}, {"AZURE_OPENAI_API_KEY": ""}))


if __name__ == "__main__":
    unittest.main()
