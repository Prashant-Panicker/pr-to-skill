"""Durable artifact storage adapters for local runs and Azure Functions."""

import json
import os
import tempfile

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient


class LocalArtifactStore:
    def __init__(self, output_dir: str):
        self._output_dir = output_dir

    def read_json(self, name: str):
        path = os.path.join(self._output_dir, name)
        if not os.path.exists(path):
            return None
        with open(path) as source:
            return json.load(source)

    def write_text(self, name: str, content: str) -> str:
        path = os.path.join(self._output_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=os.path.dirname(path), text=True
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(content)
            os.replace(temporary_path, path)
        except Exception:
            os.unlink(temporary_path)
            raise
        return path


class AzureBlobArtifactStore:
    def __init__(
        self,
        connection_string: str | None = None,
        account_url: str | None = None,
        container_name: str = "pr-review-artifacts",
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

    def read_json(self, name: str):
        try:
            content = self._container.download_blob(name).readall()
        except ResourceNotFoundError:
            return None
        return json.loads(content)

    def write_text(self, name: str, content: str) -> str:
        self._container.upload_blob(name, content.encode("utf-8"), overwrite=True)
        return name