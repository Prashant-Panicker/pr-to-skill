import importlib
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch


if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")
    openai_stub.AzureOpenAI = object
    sys.modules["openai"] = openai_stub

try:
    import azure.identity
except ImportError:
    azure_stub = types.ModuleType("azure")
    identity_stub = types.ModuleType("azure.identity")
    identity_stub.DefaultAzureCredential = object
    identity_stub.get_bearer_token_provider = lambda credential, scope: lambda: "token"
    azure_stub.identity = identity_stub
    sys.modules["azure"] = azure_stub
    sys.modules["azure.identity"] = identity_stub

azure_client = importlib.import_module("azure_client")


class Message:
    def __init__(self, content):
        self.content = content


class Choice:
    def __init__(self, content):
        self.message = Message(content)


class Response:
    def __init__(self, choices):
        self.choices = choices


class Completions:
    def __init__(self, response):
        self.response = response

    def create(self, **request):
        return self.response


def adapter_for(response):
    sdk_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=Completions(response))
    )
    return azure_client.AzureModelClient(sdk_client, api_mode="chat_completions")


class AzureModelClientTests(unittest.TestCase):
    @patch("azure_client.get_token_provider", return_value="provider")
    @patch("azure_client.AzureOpenAI")
    def test_builds_client_like_azure_code_agent(self, azure_openai, token_provider):
        sdk_client = Mock()
        azure_openai.return_value = sdk_client

        result = azure_client.get_client(
            "https://resource.openai.azure.com/",
            "2025-03-01-preview",
            request_timeout=1800,
        )

        self.assertIsInstance(result, azure_client.AzureModelClient)
        token_provider.assert_called_once_with()
        azure_openai.assert_called_once_with(
            azure_endpoint="https://resource.openai.azure.com",
            azure_ad_token_provider="provider",
            api_version="2025-03-01-preview",
            timeout=1800,
            max_retries=2,
        )

    @patch("azure_client.get_token_provider", return_value="provider")
    @patch("azure_client.AzureOpenAI")
    def test_uses_api_key_when_provided(self, azure_openai, token_provider):
        azure_openai.return_value = Mock()

        azure_client.get_client(
            "https://resource.openai.azure.com/",
            "2025-03-01-preview",
            request_timeout=1800,
            api_key="secret-key",
        )

        # API-key auth must not touch the Azure AD token provider.
        token_provider.assert_not_called()
        azure_openai.assert_called_once_with(
            azure_endpoint="https://resource.openai.azure.com",
            api_key="secret-key",
            api_version="2025-03-01-preview",
            timeout=1800,
            max_retries=2,
        )

    @patch("azure_client.get_token_provider", return_value="provider")
    @patch("azure_client.AzureOpenAI")
    def test_supports_disabling_sdk_retries(self, azure_openai, token_provider):
        azure_openai.return_value = Mock()

        azure_client.get_client(
            "https://resource.openai.azure.com/",
            "2025-03-01-preview",
            request_timeout=210,
            max_retries=0,
        )

        self.assertEqual(azure_openai.call_args.kwargs["max_retries"], 0)

    @patch("azure_client.get_token_provider", return_value="provider")
    @patch("azure_client.AzureOpenAI")
    def test_falls_back_to_token_provider_without_api_key(
        self, azure_openai, token_provider
    ):
        azure_openai.return_value = Mock()

        azure_client.get_client(
            "https://resource.openai.azure.com/",
            "2025-03-01-preview",
            request_timeout=1800,
            api_key=None,
        )

        token_provider.assert_called_once_with()
        _, kwargs = azure_openai.call_args
        self.assertNotIn("api_key", kwargs)
        self.assertEqual(kwargs["azure_ad_token_provider"], "provider")

    def test_uses_responses_api_by_default(self):
        output_text = types.SimpleNamespace(type="output_text", text="result")
        message = types.SimpleNamespace(type="message", content=[output_text])
        responses = Mock()
        responses.create.return_value = types.SimpleNamespace(output=[message])
        sdk_client = types.SimpleNamespace(responses=responses)
        client = azure_client.AzureModelClient(sdk_client)

        result = client.call_text(
            "gpt-5.4-pro", "system prompt", "user prompt", temperature=0.2
        )

        self.assertEqual(result, "result")
        responses.create.assert_called_once_with(
            model="gpt-5.4-pro",
            input=[
                {"role": "developer", "content": "system prompt"},
                {"role": "user", "content": "user prompt"},
            ],
            max_output_tokens=30000,
        )

    def test_resolves_same_environment_overrides_as_agent(self):
        resolved = azure_client.resolve_config(
            {"endpoint": "yaml-endpoint", "deployment": "yaml-deployment"},
            {
                "ENDPOINT_URL": "https://environment-endpoint/",
                "DEPLOYMENT_NAME": "gpt-5.4-pro",
                "AZURE_OPENAI_API_VERSION": "2025-03-01-preview",
                "AZURE_OPENAI_API_MODE": "responses",
                "AGENT_REQUEST_TIMEOUT": "1800",
                "AGENT_MAX_OUTPUT_TOKENS": "30000",
            },
        )

        self.assertEqual(
            resolved,
            {
                "endpoint": "https://environment-endpoint",
                "deployment": "gpt-5.4-pro",
                "api_version": "2025-03-01-preview",
                "api_mode": "responses",
                "request_timeout": 1800,
                "max_output_tokens": 30000,
                "embedding_deployment": None,
                "embedding_dimensions": 1536,
                "api_key": None,
            },
        )

    def test_resolves_api_key_from_environment_over_config(self):
        resolved = azure_client.resolve_config(
            {
                "endpoint": "https://environment-endpoint/",
                "deployment": "deployment",
                "api_key": "config-key",
            },
            {"AZURE_OPENAI_API_KEY": "env-key"},
        )

        self.assertEqual(resolved["api_key"], "env-key")

    def test_resolves_api_key_from_config_when_env_absent(self):
        resolved = azure_client.resolve_config(
            {
                "endpoint": "https://environment-endpoint/",
                "deployment": "deployment",
                "api_key": "config-key",
            },
            {},
        )

        self.assertEqual(resolved["api_key"], "config-key")

    def test_missing_endpoint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "endpoint is required"):
            azure_client.resolve_config({"deployment": "deployment"}, {})

    def test_missing_deployment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "deployment is required"):
            azure_client.resolve_config({"endpoint": "https://e/"}, {})

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be integers"):
            azure_client.resolve_config(
                {"endpoint": "https://e/", "deployment": "d"},
                {"AGENT_REQUEST_TIMEOUT": "not-an-int"},
            )

    def test_rejects_missing_choices(self):
        client = adapter_for(Response([]))

        with self.assertRaisesRegex(ValueError, "no choices"):
            client.call_text("deployment", "system", "user", temperature=0.2)

    def test_rejects_absent_content(self):
        client = adapter_for(Response([Choice(None)]))

        with self.assertRaisesRegex(ValueError, "no output text"):
            client.call_text("deployment", "system", "user", temperature=0.2)

    def test_rejects_absent_message(self):
        client = adapter_for(Response([types.SimpleNamespace(message=None)]))

        with self.assertRaisesRegex(ValueError, "no output text"):
            client.call_text("deployment", "system", "user", temperature=0.2)

    def test_parses_json_object(self):
        client = adapter_for(Response([Choice(json.dumps({"items": []}))]))

        result = client.call_json("deployment", "system", "user")

        self.assertEqual(result, {"items": []})

    def test_returns_embeddings_in_input_order(self):
        embeddings = Mock()
        embeddings.create.return_value = types.SimpleNamespace(data=[
            types.SimpleNamespace(index=1, embedding=[0.3, 0.4]),
            types.SimpleNamespace(index=0, embedding=[0.1, 0.2]),
        ])
        client = azure_client.AzureModelClient(
            types.SimpleNamespace(embeddings=embeddings)
        )

        result = client.embed("embedding-deployment", ["first", "second"])

        self.assertEqual(result, [[0.1, 0.2], [0.3, 0.4]])
        embeddings.create.assert_called_once_with(
            model="embedding-deployment", input=["first", "second"]
        )

    def test_rejects_invalid_json(self):
        client = adapter_for(Response([Choice("not json")]))

        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            client.call_json("deployment", "system", "user")


if __name__ == "__main__":
    unittest.main()
