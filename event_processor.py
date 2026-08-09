"""Idempotent dispatch of validated one-repository webhook jobs."""

from application_ports import DeliveryStore


class EventProcessor:
    def __init__(
        self, pipeline, delivery_store: DeliveryStore, allowed_repo: str,
        allowed_work_types: set[str] | None = None,
    ):
        self._pipeline = pipeline
        self._delivery_store = delivery_store
        self._allowed_repo = allowed_repo.lower()
        self._allowed_work_types = allowed_work_types or {"review", "mining"}

    def process(self, job: dict):
        if job.get("version") != 1:
            raise ValueError("Unsupported webhook job version")
        delivery_id = job.get("delivery_id")
        if not delivery_id:
            raise ValueError("Webhook job delivery ID is required")
        if job.get("repo", "").lower() != self._allowed_repo:
            raise ValueError("Webhook repository is not configured for this deployment")
        with self._delivery_store.lock(f"delivery-{delivery_id}") as lease:
            if self._delivery_store.is_completed(delivery_id):
                return None

            work_type = job.get("work_type")
            if work_type not in self._allowed_work_types:
                raise ValueError(f"Work type {work_type!r} is not allowed in this worker")
            if work_type == "review":
                result = self._pipeline.analyze_pull_request(job["repo"], job["pr_number"])
            elif work_type == "mining":
                result = self._pipeline.mine_pull_request(job["repo"], job["pr_number"])
            else:
                raise ValueError(f"Unsupported webhook work type: {work_type!r}")
            lease.ensure_active()
            self._delivery_store.mark_completed(delivery_id)
            return result