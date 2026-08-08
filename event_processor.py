"""Idempotent dispatch of validated one-repository webhook jobs."""


class EventProcessor:
    def __init__(self, pipeline, delivery_store, allowed_repo: str):
        self._pipeline = pipeline
        self._delivery_store = delivery_store
        self._allowed_repo = allowed_repo.lower()

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
            if work_type == "analysis":
                result = self._pipeline.analyze_pull_request(job["repo"], job["pr_number"])
            elif work_type == "history":
                result = self._pipeline.reconcile_feedback(job["repo"], job["pr_number"])
            else:
                raise ValueError(f"Unsupported webhook work type: {work_type!r}")
            lease.ensure_active()
            self._delivery_store.mark_completed(delivery_id)
            return result