"""Azure-specific composition for the queue-triggered demo worker."""

import os

import yaml

import azure_client
import vector_store
from artifact_store import AzureBlobArtifactStore
from azure_delivery_store import AzureBlobDeliveryStore
from event_processor import EventProcessor
from incremental_pipeline import IncrementalPipeline


def build_event_processor() -> EventProcessor:
    config_path = os.environ.get("PR_TO_SKILL_CONFIG", "config.yaml")
    with open(config_path) as source:
        config = yaml.safe_load(source)
    configured_repo = os.environ.get("PR_TO_SKILL_REPO")
    configured_reviewer = os.environ.get("PR_TO_SKILL_REVIEWER")
    if configured_repo:
        config["repos"] = [configured_repo]
    if configured_reviewer:
        config["person"]["github_username"] = configured_reviewer
    repos = config.get("repos", [])
    if len(repos) != 1:
        raise ValueError("The Azure demo deployment requires exactly one configured repository")
    search_config = vector_store.resolve_config(config.get("azure_search", {}))
    if not search_config["enabled"]:
        raise ValueError("The Azure webhook worker requires azure_search.enabled: true")
    openai_config = azure_client.resolve_config(config["azure_openai"])
    worker_timeout = int(os.environ.get("PR_TO_SKILL_OPENAI_TIMEOUT", "600"))
    if worker_timeout <= 0 or worker_timeout > 600:
        raise ValueError("PR_TO_SKILL_OPENAI_TIMEOUT must be between 1 and 600 seconds")
    client = azure_client.get_client(
        endpoint=openai_config["endpoint"],
        api_version=openai_config["api_version"],
        request_timeout=worker_timeout,
        api_mode=openai_config["api_mode"],
        max_output_tokens=openai_config["max_output_tokens"],
        api_key=openai_config.get("api_key"),
    )
    store = vector_store.create_store(search_config, client)
    storage_connection = os.environ.get("AzureWebJobsStorage")
    storage_account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not storage_connection and not storage_account_url:
        raise ValueError("AzureWebJobsStorage or AZURE_STORAGE_ACCOUNT_URL is required")
    receipts = AzureBlobDeliveryStore(storage_connection, storage_account_url)
    artifacts = AzureBlobArtifactStore(storage_connection, storage_account_url)
    pipeline = IncrementalPipeline(
        client, openai_config["deployment"], store, config,
        artifact_store=artifacts, reconciliation_lock=receipts,
        deadline_seconds=int(os.environ.get("PR_TO_SKILL_WORKFLOW_DEADLINE", "2700")),
    )
    return EventProcessor(pipeline, receipts, repos[0])