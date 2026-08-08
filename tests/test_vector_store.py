import unittest
from unittest.mock import Mock

import vector_store


def note():
    return {
        "repo": "org/repo", "pr_number": 42, "github_comment_id": 9001,
        "comment_type": "review_comment", "pr_url": "https://example/pull/42",
        "file_path": "api.py", "original_body": "Validate this input.",
        "category": "security", "severity": "blocking",
        "original_issue": "Input was trusted.",
        "requested_change": "Validate the input.",
        "rationale": "Untrusted data must be rejected.",
    }


def store_for(search_client, embedding_client):
    search_client.indices.exists.return_value = True
    return vector_store.OpenSearchReviewNoteStore(
        search_client, embedding_client, "embedding", "review-notes", 2
    )


class VectorStoreTests(unittest.TestCase):
    def test_creates_knn_index_when_absent(self):
        search_client = Mock()
        search_client.indices.exists.return_value = False

        vector_store.OpenSearchReviewNoteStore(
            search_client, Mock(), "embedding", "review-notes", 2
        )

        mapping = search_client.indices.create.call_args.kwargs["body"]
        self.assertTrue(mapping["settings"]["index"]["knn"])
        self.assertEqual(
            mapping["mappings"]["properties"]["content_vector"]["dimension"], 2
        )

    def test_saves_searchable_note_with_stable_id_and_metadata(self):
        search_client = Mock()
        search_client.bulk.return_value = {"errors": False}
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = store_for(search_client, embedding_client)

        count = store.save_notes([note()], "reviewer")

        self.assertEqual(count, 1)
        body = search_client.bulk.call_args.kwargs["body"]
        self.assertEqual(len(body[0]["index"]["_id"]), 64)
        self.assertEqual(body[1]["reviewer"], "reviewer")
        self.assertEqual(body[1]["category"], "security")
        self.assertIn("Validate the input.", body[1]["content"])

    def test_raises_when_opensearch_rejects_a_document(self):
        search_client = Mock()
        search_client.bulk.return_value = {"errors": True}
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = store_for(search_client, embedding_client)

        with self.assertRaisesRegex(RuntimeError, "rejected"):
            store.save_notes([note()], "reviewer")

    def test_search_uses_vector_and_repository_filters(self):
        search_client = Mock()
        search_client.search.return_value = {
            "hits": {"hits": [{"_source": {"repo": "org/repo", "pr_number": 42}}]}
        }
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = store_for(search_client, embedding_client)

        results = store.search(
            "authorization", "org/repo", limit=3, reviewer="reviewer"
        )

        self.assertEqual(results, [{"repo": "org/repo", "pr_number": 42}])
        request = search_client.search.call_args.kwargs
        knn = request["body"]["query"]["knn"]["content_vector"]
        self.assertEqual(knn["k"], 3)
        self.assertEqual(knn["vector"], [0.1, 0.2])
        self.assertEqual(knn["filter"]["bool"]["filter"], [
            {"term": {"repo": "org/repo"}},
            {"term": {"reviewer": "reviewer"}},
        ])

    def test_resolve_config_requires_vector_dependencies_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "embedding_deployment"):
            vector_store.resolve_config(
                {"enabled": True, "endpoint": "search.example"}, {}
            )


if __name__ == "__main__":
    unittest.main()
