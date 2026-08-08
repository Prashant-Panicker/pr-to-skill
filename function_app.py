"""Azure Functions entry points for GitHub webhook ingestion and processing."""

import json
import os

import azure.functions as func

from azure_runtime import build_event_processor
from webhook_handler import route_delivery, verify_signature


app = func.FunctionApp()


@app.function_name(name="GitHubWebhook")
@app.route(route="github/webhook", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.queue_output(
    arg_name="event_message",
    queue_name="pr-review-events",
    connection="AzureWebJobsStorage",
)
def github_webhook(req: func.HttpRequest, event_message: func.Out[str]) -> func.HttpResponse:
    body = req.get_body()
    if not verify_signature(
        os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
        body,
        req.headers.get("X-Hub-Signature-256"),
    ):
        return func.HttpResponse("Invalid webhook signature", status_code=401)
    try:
        job = route_delivery(
            req.headers.get("X-GitHub-Delivery", ""),
            req.headers.get("X-GitHub-Event", ""),
            body,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return func.HttpResponse(str(exc), status_code=400)
    if job is not None:
        event_message.set(json.dumps(job))
    return func.HttpResponse(status_code=202)


@app.function_name(name="ProcessPullRequestEvent")
@app.queue_trigger(
    arg_name="message",
    queue_name="pr-review-events",
    connection="AzureWebJobsStorage",
)
def process_pull_request_event(message: func.QueueMessage) -> None:
    job = json.loads(message.get_body().decode("utf-8"))
    build_event_processor().process(job)