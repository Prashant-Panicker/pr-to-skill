"""AWS Lambda entry points for webhook ingress and queued processing."""

import base64
import json
import logging
import os

from aws_runtime import (
    build_event_processor,
    build_job_publisher,
    configure_runtime_secrets,
)
from webhook_handler import route_delivery, verify_signature

logger = logging.getLogger(__name__)


def webhook(event, context):
    del context
    configure_runtime_secrets()
    body_text = event.get("body") or ""
    body = (
        base64.b64decode(body_text)
        if event.get("isBase64Encoded")
        else body_text.encode("utf-8")
    )
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    if not verify_signature(
        os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
        body,
        headers.get("x-hub-signature-256"),
    ):
        return {"statusCode": 401, "body": "Invalid webhook signature"}
    try:
        job = route_delivery(
            headers.get("x-github-delivery", ""),
            headers.get("x-github-event", ""),
            body,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return {"statusCode": 400, "body": str(exc)}
    if job is not None:
        build_job_publisher().publish(job)
    return {"statusCode": 202, "body": ""}


def _process_events(event, work_type: str):
    processor = build_event_processor(work_type)
    failures = []
    for record in event.get("Records", []):
        try:
            processor.process(json.loads(record["body"]))
        except Exception:
            logger.exception("Failed to process SQS message %s", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def process_reviews(event, context):
    del context
    return _process_events(event, "review")


def process_mining(event, context):
    del context
    return _process_events(event, "mining")