"""Azure Blob Storage receipts for successfully processed GitHub deliveries."""

import threading
from contextlib import contextmanager

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobLeaseClient, BlobServiceClient


class LeaseGuard:
    def __init__(self, renewal_errors: list[Exception]):
        self._renewal_errors = renewal_errors

    def ensure_active(self) -> None:
        if self._renewal_errors:
            raise RuntimeError("Azure Blob lease renewal failed") from self._renewal_errors[0]


class AzureBlobDeliveryStore:
    def __init__(
        self,
        connection_string: str | None = None,
        account_url: str | None = None,
        container_name: str = "webhook-receipts",
    ):
        if connection_string:
            service = BlobServiceClient.from_connection_string(connection_string)
        elif account_url:
            service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        else:
            raise ValueError("Azure Blob connection string or account URL is required")
        self._container = service.get_container_client(container_name)
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass

    def is_completed(self, delivery_id: str) -> bool:
        try:
            self._container.get_blob_client(f"completed/{delivery_id}").get_blob_properties()
            return True
        except ResourceNotFoundError:
            return False

    def mark_completed(self, delivery_id: str) -> None:
        self._container.upload_blob(
            f"completed/{delivery_id}", b"completed", overwrite=True
        )

    @contextmanager
    def lock(self, name: str):
        blob = self._container.get_blob_client(f"locks/{name}")
        try:
            blob.upload_blob(b"lock", overwrite=False)
        except ResourceExistsError:
            pass
        lease = BlobLeaseClient(blob)
        lease.acquire(lease_duration=60)
        stopped = threading.Event()
        renewal_errors: list[Exception] = []

        def renew() -> None:
            while not stopped.wait(20):
                try:
                    lease.renew()
                except Exception as exc:
                    renewal_errors.append(exc)
                    stopped.set()

        renewer = threading.Thread(target=renew, name="blob-lease-renewer", daemon=True)
        renewer.start()
        guard = LeaseGuard(renewal_errors)
        try:
            yield guard
        finally:
            stopped.set()
            renewer.join(timeout=5)
            lease.release()
        guard.ensure_active()