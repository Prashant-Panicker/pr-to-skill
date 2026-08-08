import unittest
from contextlib import nullcontext
from unittest.mock import Mock

from event_processor import EventProcessor


class EventProcessorTests(unittest.TestCase):
    def test_dispatches_analysis_and_marks_delivery_after_success(self):
        pipeline = Mock()
        pipeline.analyze_pull_request.return_value = "review.md"
        receipts = Mock()
        lease = Mock()
        receipts.lock.return_value = nullcontext(lease)
        receipts.is_completed.return_value = False
        processor = EventProcessor(pipeline, receipts, "org/repo")

        result = processor.process({
            "version": 1, "delivery_id": "delivery-1", "work_type": "analysis",
            "repo": "org/repo", "pr_number": 12,
        })

        self.assertEqual(result, "review.md")
        lease.ensure_active.assert_called_once_with()
        receipts.lock.assert_called_once_with("delivery-delivery-1")
        receipts.mark_completed.assert_called_once_with("delivery-1")

    def test_skips_completed_delivery(self):
        pipeline = Mock()
        receipts = Mock()
        receipts.lock.return_value = nullcontext(Mock())
        receipts.is_completed.return_value = True
        processor = EventProcessor(pipeline, receipts, "org/repo")

        result = processor.process({
            "version": 1, "delivery_id": "delivery-1", "work_type": "history",
            "repo": "org/repo", "pr_number": 12,
        })

        self.assertIsNone(result)
        pipeline.reconcile_feedback.assert_not_called()
        receipts.mark_completed.assert_not_called()

    def test_does_not_complete_delivery_after_lease_loss(self):
        pipeline = Mock()
        receipts = Mock()
        lease = Mock()
        lease.ensure_active.side_effect = RuntimeError("lease lost")
        receipts.lock.return_value = nullcontext(lease)
        receipts.is_completed.return_value = False
        processor = EventProcessor(pipeline, receipts, "org/repo")

        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            processor.process({
                "version": 1, "delivery_id": "delivery-1", "work_type": "history",
                "repo": "org/repo", "pr_number": 12,
            })

        receipts.mark_completed.assert_not_called()

    def test_rejects_another_repository(self):
        processor = EventProcessor(Mock(), Mock(), "org/repo")

        with self.assertRaisesRegex(ValueError, "not configured"):
            processor.process({
                "version": 1, "delivery_id": "delivery-1", "work_type": "analysis",
                "repo": "other/repo", "pr_number": 12,
            })


if __name__ == "__main__":
    unittest.main()