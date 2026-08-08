"""Cloud-neutral ports owned by the PR review application."""

from contextlib import AbstractContextManager
from typing import Protocol


class LeaseGuard(Protocol):
    def ensure_active(self) -> None: ...


class ArtifactStore(Protocol):
    def read_json(self, name: str): ...

    def write_text(self, name: str, content: str) -> str: ...


class DeliveryStore(Protocol):
    def is_completed(self, delivery_id: str) -> bool: ...

    def mark_completed(self, delivery_id: str) -> None: ...

    def lock(self, name: str) -> AbstractContextManager[LeaseGuard]: ...


class ModelClient(Protocol):
    def call_text(
        self,
        deployment: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float,
        response_format: dict[str, str] | None = None,
    ) -> str: ...

    def embed(self, deployment: str, texts: list[str]) -> list[list[float]]: ...


class ReviewNoteStore(Protocol):
    def save_notes(self, notes: list[dict], reviewer: str) -> int: ...

    def delete_notes(self, notes: list[dict]) -> int: ...

    def search(
        self, query: str, repo: str, limit: int = 5, reviewer: str | None = None
    ) -> list[dict]: ...


class JobPublisher(Protocol):
    def publish(self, job: dict) -> None: ...