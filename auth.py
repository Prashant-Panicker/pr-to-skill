"""Azure OpenAI authentication.

Two authentication methods are supported:

* **API key** — if an Azure OpenAI API key is configured (via the
  ``AZURE_OPENAI_API_KEY`` environment variable or ``azure_openai.api_key`` in
  the config), it is used directly. The key is never logged or persisted.
* **Azure AD / Entra ID** — when no API key is configured, the existing
  ``DefaultAzureCredential`` chain is used, which picks up an ``az login``
  session (or a managed identity / environment credentials where available).
"""

import os

from azure.identity import DefaultAzureCredential, get_bearer_token_provider

_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"


def get_api_key(config: dict, environ: dict[str, str] | None = None) -> str | None:
    """Return a configured Azure OpenAI API key, if any.

    The environment variable takes precedence over the config file so secrets
    can be supplied without being written to disk. Returns ``None`` when no key
    is configured, signalling that Azure AD authentication should be used.
    """
    env = os.environ if environ is None else environ
    api_key = env.get("AZURE_OPENAI_API_KEY") or config.get("api_key")
    if api_key is None:
        return None
    api_key = api_key.strip()
    return api_key or None


def get_token_provider():
    """Return the refreshing bearer-token provider expected by ``AzureOpenAI``."""
    credential = DefaultAzureCredential(
        exclude_environment_credential=False,
        exclude_managed_identity_credential=True,
        exclude_shared_token_cache_credential=False,
        exclude_visual_studio_code_credential=True,
        exclude_cli_credential=False,
    )
    return get_bearer_token_provider(credential, _TOKEN_SCOPE)
