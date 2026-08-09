"""Validation and routing for GitHub webhook deliveries."""

import hashlib
import hmac
import json


ANALYSIS_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
MINING_PULL_REQUEST_ACTIONS = {"closed", "edited"}
HISTORY_EVENTS = {
    "pull_request_review": {"submitted", "edited", "dismissed"},
    "pull_request_review_comment": {"created", "edited", "deleted"},
    "issue_comment": {"created", "edited", "deleted"},
}


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, f"sha256={expected}")


def route_delivery(delivery_id: str, event: str, body: bytes) -> dict | None:
    if not delivery_id:
        raise ValueError("GitHub delivery ID is required")
    payload = json.loads(body)
    action = payload.get("action")
    if event == "pull_request" and action in ANALYSIS_ACTIONS:
        work_type = "review"
    elif event == "pull_request" and action in MINING_PULL_REQUEST_ACTIONS:
        work_type = "mining"
    elif (
        action in HISTORY_EVENTS.get(event, set())
        and (event != "issue_comment" or payload.get("issue", {}).get("pull_request"))
    ):
        work_type = "mining"
    else:
        return None
    repository = payload.get("repository", {}).get("full_name")
    pull_request = payload.get("pull_request", {})
    pr_number = (
        pull_request.get("number")
        or payload.get("issue", {}).get("number")
        or payload.get("number")
    )
    if not repository or not isinstance(pr_number, int):
        raise ValueError("Webhook repository and pull request number are required")
    return {
        "version": 1,
        "delivery_id": delivery_id,
        "work_type": work_type,
        "event": event,
        "action": action,
        "repo": repository,
        "pr_number": pr_number,
    }