import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import vector_store


def note():
    return {
        "repo": "org/repo", "pr_number": 42, "github_comment_id": 9001,
        "comment_type": "review_comment",
        "pr_url": "https://example/pull/42",
        "file_path": "api.py", "original_body": "Validate this input.",
        "category": "security", "severity": "blocking",
        "original_issue": "Input was trusted.",
        "requested_change": "Validate the input.",
        "rationale": "Untrusted data must be rejected.",
    }


class VectorStoreTests(unittest.TestCase):
    def test_saves_searchable_note_with_stable_id_and_metadata(self):
        search_client = Mock()
        search_client.upload_documents.return_value = [SimpleNamespace(succeeded=True)]
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = vector_store.AzureReviewNoteStore(search_client, embedding_client, "embedding")

        count = store.save_notes([note()], "reviewer")

        self.assertEqual(count, 1)
        document = search_client.upload_documents.call_args.args[0][0]
        self.assertEqual(len(document["id"]), 64)
        self.assertEqual(document["reviewer"], "reviewer")
        self.assertEqual(document["category"], "security")
        self.assertIn("Validate the input.", document["content"])

    def test_raises_when_search_rejects_a_document(self):
        search_client = Mock()
        search_client.upload_documents.return_value = [SimpleNamespace(succeeded=False)]
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = vector_store.AzureReviewNoteStore(search_client, embedding_client, "embedding")

        with self.assertRaisesRegex(RuntimeError, "rejected 1"):
            store.save_notes([note()], "reviewer")

    def test_search_uses_hybrid_query_and_reviewer_filter(self):
        search_client = Mock()
        search_client.search.return_value = [{"repo": "org/repo", "pr_number": 42}]
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = vector_store.AzureReviewNoteStore(search_client, embedding_client, "embedding")

        results = store.search(
            "authorization", "org/repo", limit=3, reviewer="o'reviewer"
        )

        self.assertEqual(results, [{"repo": "org/repo", "pr_number": 42}])
        request = search_client.search.call_args.kwargs
        self.assertEqual(request["search_text"], "authorization")
        self.assertEqual(request["top"], 3)
        self.assertEqual(
            request["filter"],
            "repo eq 'org/repo' and reviewer eq 'o''reviewer'",
        )
        self.assertEqual(request["vector_queries"][0].k_nearest_neighbors, 3)

    def test_resolve_config_requires_vector_dependencies_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "embedding_deployment"):
            vector_store.resolve_config(
                {"enabled": True, "endpoint": "https://search.example"}, {}
            )


if __name__ == "__main__":
    unittest.main()