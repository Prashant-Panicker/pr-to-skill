"""AWS implementations of application-owned storage and messaging ports."""

import json
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal

from botocore.exceptions import ClientError


class S3ArtifactStore:
    def __init__(self, s3_client, bucket: str, prefix: str = "artifacts"):
        self._client = s3_client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def _key(self, name: str) -> str:
        return f"{self._prefix}/{name}" if self._prefix else name

    def read_json(self, name: str):
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key(name))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return json.loads(response["Body"].read())

    def write_text(self, name: str, content: str) -> str:
        key = self._key(name)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="application/json" if name.endswith(".json") else "text/markdown",
            ServerSideEncryption="AES256",
        )
        return f"s3://{self._bucket}/{key}"


class DynamoLeaseGuard:
    def __init__(self, renewal_errors: list[Exception]):
        self._renewal_errors = renewal_errors

    def ensure_active(self) -> None:
        if self._renewal_errors:
            raise RuntimeError("DynamoDB lease renewal failed") from self._renewal_errors[0]


class DynamoDeliveryStore:
    def __init__(self, table, lease_seconds: int = 60):
        if lease_seconds < 10:
            raise ValueError("lease_seconds must be at least 10")
        self._table = table
        self._lease_seconds = lease_seconds

    def is_completed(self, delivery_id: str) -> bool:
        response = self._table.get_item(
            Key={"key": f"DELIVERY#{delivery_id}"}, ConsistentRead=True
        )
        return response.get("Item", {}).get("state") == "completed"

    def mark_completed(self, delivery_id: str) -> None:
        self._table.put_item(Item={
            "key": f"DELIVERY#{delivery_id}",
            "state": "completed",
            "completed_at": Decimal(str(time.time())),
        })

    @contextmanager
    def lock(self, name: str):
        key = {"key": f"LOCK#{name}"}
        owner = str(uuid.uuid4())
        now = int(time.time())
        self._table.put_item(
            Item={**key, "owner": owner, "expires_at": now + self._lease_seconds},
            ConditionExpression="attribute_not_exists(#key) OR expires_at < :now",
            ExpressionAttributeNames={"#key": "key"},
            ExpressionAttributeValues={":now": now},
        )
        stopped = threading.Event()
        renewal_errors: list[Exception] = []

        def renew() -> None:
            while not stopped.wait(self._lease_seconds / 3):
                try:
                    self._table.update_item(
                        Key=key,
                        UpdateExpression="SET expires_at = :expires_at",
                        ConditionExpression="#owner = :owner",
                        ExpressionAttributeNames={"#owner": "owner"},
                        ExpressionAttributeValues={
                            ":expires_at": int(time.time()) + self._lease_seconds,
                            ":owner": owner,
                        },
                    )
                except Exception as exc:
                    renewal_errors.append(exc)
                    stopped.set()

        renewer = threading.Thread(target=renew, name="dynamodb-lease-renewer", daemon=True)
        renewer.start()
        guard = DynamoLeaseGuard(renewal_errors)
        try:
            yield guard
        finally:
            stopped.set()
            renewer.join(timeout=5)
            try:
                self._table.delete_item(
                    Key=key,
                    ConditionExpression="#owner = :owner",
                    ExpressionAttributeNames={"#owner": "owner"},
                    ExpressionAttributeValues={":owner": owner},
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
        guard.ensure_active()


class SqsJobPublisher:
    def __init__(self, sqs_client, queue_url: str):
        self._client = sqs_client
        self._queue_url = queue_url

    def publish(self, job: dict) -> None:
        self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(job, separators=(",", ":")),
        )