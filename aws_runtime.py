"""AWS host composition; application modules remain provider-neutral."""

import json
import os
from functools import lru_cache

import boto3
import yaml

import azure_client
import vector_store
from aws_adapters import DynamoDeliveryStore, S3ArtifactStore, SqsJobPublisher
from event_processor import EventProcessor
from incremental_pipeline import IncrementalPipeline


@lru_cache(maxsize=None)
def load_secret(secret_id: str) -> str:
    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
    value = response.get("SecretString")
    if not value:
        raise ValueError(f"Secret {secret_id!r} must contain a string value")
    return value


def configure_runtime_secrets() -> None:
    mappings = {
        "GITHUB_WEBHOOK_SECRET_ID": "GITHUB_WEBHOOK_SECRET",
        "GITHUB_APP_PRIVATE_KEY_SECRET_ID": "GITHUB_APP_PRIVATE_KEY",
        "AZURE_OPENAI_API_KEY_SECRET_ID": "AZURE_OPENAI_API_KEY",
    }
    for secret_id_setting, destination in mappings.items():
        secret_id = os.environ.get(secret_id_setting)
        if secret_id:
            os.environ[destination] = load_secret(secret_id)


def load_config() -> dict:
    config_path = os.environ.get("PR_TO_SKILL_CONFIG", "config.example.yaml")
    with open(config_path) as source:
        config = yaml.safe_load(source)
    configured_repo = os.environ.get("PR_TO_SKILL_REPO")
    configured_reviewer = os.environ.get("PR_TO_SKILL_REVIEWER")
    if configured_repo:
        config["repos"] = [configured_repo]
    if configured_reviewer:
        config["person"]["github_username"] = configured_reviewer
    return config


@lru_cache(maxsize=1)
def build_job_publisher() -> SqsJobPublisher:
    return SqsJobPublisher(boto3.client("sqs"), os.environ["EVENT_QUEUE_URL"])


@lru_cache(maxsize=1)
def build_event_processor() -> EventProcessor:
    configure_runtime_secrets()
    config = load_config()
    repos = config.get("repos", [])
    if len(repos) != 1:
        raise ValueError("The AWS demo deployment requires exactly one configured repository")
    openai_config = azure_client.resolve_config(config["azure_openai"])
    search_config = vector_store.resolve_config(
        config.get("aws_opensearch", {}),
        embedding_deployment=openai_config.get("embedding_deployment"),
        embedding_dimensions=openai_config["embedding_dimensions"],
    )
    if not search_config["enabled"]:
        raise ValueError("The AWS worker requires aws_opensearch.enabled: true")
    request_timeout = int(os.environ.get("PR_TO_SKILL_OPENAI_TIMEOUT", "600"))
    if request_timeout <= 0 or request_timeout > 600:
        raise ValueError("PR_TO_SKILL_OPENAI_TIMEOUT must be between 1 and 600 seconds")
    workflow_deadline = int(os.environ.get("PR_TO_SKILL_WORKFLOW_DEADLINE", "780"))
    if workflow_deadline <= 0 or workflow_deadline > 780:
        raise ValueError("PR_TO_SKILL_WORKFLOW_DEADLINE must be between 1 and 780 seconds")
    client = azure_client.get_client(
        endpoint=openai_config["endpoint"],
        api_version=openai_config["api_version"],
        request_timeout=request_timeout,
        api_mode=openai_config["api_mode"],
        max_output_tokens=openai_config["max_output_tokens"],
        api_key=openai_config.get("api_key"),
    )
    store = vector_store.create_store(search_config, client)
    artifacts = S3ArtifactStore(
        boto3.client("s3"), os.environ["ARTIFACT_BUCKET"],
        os.environ.get("ARTIFACT_PREFIX", "artifacts"),
    )
    delivery_store = DynamoDeliveryStore(
        boto3.resource("dynamodb").Table(os.environ["DELIVERY_TABLE"])
    )
    pipeline = IncrementalPipeline(
        client,
        openai_config["deployment"],
        store,
        config,
        artifact_store=artifacts,
        reconciliation_lock=delivery_store,
        deadline_seconds=workflow_deadline,
    )
    return EventProcessor(pipeline, delivery_store, repos[0])


def bootstrap_payload(raw_comments: list[dict], notes: list[dict]) -> str:
    return json.dumps(
        {"version": 1, "raw_comments": raw_comments, "notes": notes}, indent=2
    )