"""Validated model adapter using the same Azure client/auth pattern as azure_code_agent."""

import json
import os
from typing import Any

from openai import AzureOpenAI

from auth import get_api_key, get_token_provider


class AzureModelClient:
    """The single adapter used for all Azure OpenAI model calls."""

    def __init__(
        self,
        client: AzureOpenAI,
        api_mode: str = "responses",
        max_output_tokens: int = 30000,
    ):
        if api_mode not in {"responses", "chat_completions"}:
            raise ValueError("api_mode must be 'responses' or 'chat_completions'")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self._client = client
        self._api_mode = api_mode
        self._max_output_tokens = max_output_tokens

    @staticmethod
    def _response_text(response) -> str:
        text_parts: list[str] = []
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", None) or []:
                if getattr(content, "type", None) == "output_text":
                    text = getattr(content, "text", None)
                    if isinstance(text, str):
                        text_parts.append(text)
        return "".join(text_parts)

    def call_text(
        self,
        deployment: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float,
        response_format: dict[str, str] | None = None,
    ) -> str:
        if self._api_mode == "responses":
            response = self._client.responses.create(
                model=deployment,
                input=[
                    {"role": "developer", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_output_tokens=self._max_output_tokens,
            )
            content = self._response_text(response)
        else:
            request: dict[str, Any] = {
                "model": deployment,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_completion_tokens": self._max_output_tokens,
            }
            if response_format is not None:
                request["response_format"] = response_format

            response = self._client.chat.completions.create(**request)
            choices = getattr(response, "choices", None)
            if not choices:
                raise ValueError("Azure OpenAI returned no choices")
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)

        if not isinstance(content, str) or not content.strip():
            raise ValueError("Azure OpenAI returned no output text")
        return content

    def call_json(
        self,
        deployment: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
    ) -> dict:
        response_format = (
            {"type": "json_object"} if self._api_mode == "chat_completions" else None
        )
        content = self.call_text(
            deployment,
            system_prompt,
            user_prompt,
            temperature=temperature,
            response_format=response_format,
        )
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("Azure OpenAI returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Azure OpenAI JSON response must be an object")
        return value

    def embed(self, deployment: str, texts: list[str]) -> list[list[float]]:
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("embedding input must contain non-empty strings")
        response = self._client.embeddings.create(model=deployment, input=texts)
        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("Azure OpenAI returned an invalid embedding response")
        ordered = sorted(data, key=lambda item: item.index)
        return [item.embedding for item in ordered]


def get_client(
    endpoint: str,
    api_version: str,
    request_timeout: int = 1800,
    api_mode: str = "responses",
    max_output_tokens: int = 30000,
    api_key: str | None = None,
) -> AzureModelClient:
    if request_timeout <= 0:
        raise ValueError("request_timeout must be a positive integer")
    client_kwargs: dict[str, Any] = {
        "azure_endpoint": endpoint.rstrip("/"),
        "api_version": api_version,
        "timeout": request_timeout,
        "max_retries": 2,
    }
    # Prefer an explicit API key when configured; otherwise fall back to the
    # Azure AD / Entra ID token provider (e.g. an ``az login`` session).
    if api_key:
        client_kwargs["api_key"] = api_key
    else:
        client_kwargs["azure_ad_token_provider"] = get_token_provider()
    client = AzureOpenAI(**client_kwargs)
    return AzureModelClient(
        client,
        api_mode=api_mode,
        max_output_tokens=max_output_tokens,
    )


def resolve_config(config: dict, environ: dict[str, str] | None = None) -> dict:
    """Apply the same Azure environment overrides and defaults as azure_code_agent."""
    env = os.environ if environ is None else environ
    endpoint = env.get("AZURE_OPENAI_ENDPOINT") or env.get("ENDPOINT_URL") or config.get("endpoint")
    deployment = (
        env.get("AZURE_OPENAI_DEPLOYMENT")
        or env.get("DEPLOYMENT_NAME")
        or config.get("deployment")
    )
    if not endpoint:
        raise ValueError("Azure OpenAI endpoint is required")
    if not deployment:
        raise ValueError("Azure OpenAI deployment is required")

    try:
        request_timeout = int(
            env.get("AGENT_REQUEST_TIMEOUT", config.get("request_timeout", 1800))
        )
        max_output_tokens = int(
            env.get("AGENT_MAX_OUTPUT_TOKENS", config.get("max_output_tokens", 30000))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Azure timeout and output-token settings must be integers") from exc
    if request_timeout <= 0 or max_output_tokens <= 0:
        raise ValueError("Azure timeout and output-token settings must be positive")

    return {
        "endpoint": endpoint.rstrip("/"),
        "deployment": deployment,
        "api_version": env.get(
            "AZURE_OPENAI_API_VERSION",
            config.get("api_version", "2025-03-01-preview"),
        ),
        "api_mode": env.get(
            "AZURE_OPENAI_API_MODE", config.get("api_mode", "responses")
        ),
        "request_timeout": request_timeout,
        "max_output_tokens": max_output_tokens,
        # ``None`` means "use Azure AD authentication". The key itself is kept
        # out of logs and never echoed back by the pipeline.
        "api_key": get_api_key(config, env),
    }
