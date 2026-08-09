import unittest
from unittest.mock import Mock, patch

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
        "reviewer": "reviewer",
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

        count = store.save_notes([note()])

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
            store.save_notes([note()])

    def test_search_uses_vector_and_repository_filters(self):
        search_client = Mock()
        search_client.search.return_value = {
            "hits": {"hits": [{"_source": {"repo": "org/repo", "pr_number": 42}}]}
        }
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = store_for(search_client, embedding_client)

        results = store.search(
            "authorization", "org/repo", limit=3,
            reviewers=["reviewer", "architect"],
        )

        self.assertEqual(results, [{"repo": "org/repo", "pr_number": 42}])
        request = search_client.search.call_args.kwargs
        knn = request["body"]["query"]["knn"]["content_vector"]
        self.assertEqual(knn["k"], 3)
        self.assertEqual(knn["vector"], [0.1, 0.2])
        self.assertEqual(knn["filter"]["bool"]["filter"], [
            {"term": {"repo": "org/repo"}},
            {"terms": {"reviewer": ["reviewer", "architect"]}},
        ])

    def test_replace_notes_deletes_repository_scope_before_saving(self):
        search_client = Mock()
        search_client.delete_by_query.return_value = {"failures": []}
        search_client.bulk.return_value = {"errors": False}
        embedding_client = Mock()
        embedding_client.embed.return_value = [[0.1, 0.2]]
        store = store_for(search_client, embedding_client)

        count = store.replace_notes([note()], ["org/repo"])

        self.assertEqual(count, 1)
        self.assertEqual(
            search_client.delete_by_query.call_args.kwargs["body"],
            {"query": {"bool": {
                "filter": [{"terms": {"repo": ["org/repo"]}}],
                "must_not": [{"ids": {"values": [store._id(note())]}}],
            }}},
        )
        search_client.bulk.assert_called_once()

    def test_resolve_config_requires_vector_dependencies_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "embedding_deployment"):
            vector_store.resolve_config(
                {"enabled": True, "endpoint": "search.example"}, {},
                embedding_deployment=None,
            )

    def test_resolve_config_supports_unsigned_local_http_endpoint(self):
        resolved = vector_store.resolve_config(
            {"enabled": True},
            {
                "AWS_OPENSEARCH_ENDPOINT": (
                    "http://review.us-east-1.opensearch.localhost.localstack.cloud:4566"
                ),
                "AWS_OPENSEARCH_SIGN_REQUESTS": "false",
                "AWS_OPENSEARCH_VERIFY_CERTS": "false",
            },
            embedding_deployment="embedding",
            embedding_dimensions=2,
        )

        self.assertEqual(
            resolved["host"],
            "review.us-east-1.opensearch.localhost.localstack.cloud",
        )
        self.assertEqual(resolved["port"], 4566)
        self.assertFalse(resolved["use_ssl"])
        self.assertFalse(resolved["verify_certs"])
        self.assertFalse(resolved["sign_requests"])

    def test_resolve_config_rejects_invalid_transport_boolean(self):
        with self.assertRaisesRegex(
            ValueError, "AWS_OPENSEARCH_SIGN_REQUESTS must be true or false"
        ):
            vector_store.resolve_config(
                {"enabled": True, "endpoint": "http://localhost:4566"},
                {"AWS_OPENSEARCH_SIGN_REQUESTS": "sometimes"},
                embedding_deployment="embedding",
            )

    def test_resolve_config_rejects_endpoint_path(self):
        with self.assertRaisesRegex(ValueError, "must not include a path"):
            vector_store.resolve_config(
                {"enabled": True, "endpoint": "http://localhost:4566/opensearch"},
                {},
                embedding_deployment="embedding",
            )

    @patch("vector_store.OpenSearch")
    def test_create_store_uses_unsigned_local_transport(self, opensearch):
        config = vector_store.resolve_config(
            {"enabled": True},
            {
                "AWS_OPENSEARCH_ENDPOINT": "http://localhost:4566",
                "AWS_OPENSEARCH_SIGN_REQUESTS": "false",
                "AWS_OPENSEARCH_VERIFY_CERTS": "false",
            },
            embedding_deployment="embedding",
            embedding_dimensions=2,
        )
        opensearch.return_value.indices.exists.return_value = True

        vector_store.create_store(config, Mock())

        settings = opensearch.call_args.kwargs
        self.assertEqual(settings["hosts"], [{"host": "localhost", "port": 4566}])
        self.assertIsNone(settings["http_auth"])
        self.assertFalse(settings["use_ssl"])
        self.assertFalse(settings["verify_certs"])

    @patch("vector_store.OpenSearch")
    @patch("vector_store.AWSV4SignerAuth")
    @patch("vector_store.boto3.Session")
    def test_create_store_preserves_signed_aws_transport(
        self, session, signer, opensearch
    ):
        credentials = Mock()
        session.return_value.get_credentials.return_value = credentials
        auth = Mock()
        signer.return_value = auth
        opensearch.return_value.indices.exists.return_value = True
        config = vector_store.resolve_config(
            {"enabled": True, "endpoint": "collection.aoss.amazonaws.com"},
            {},
            embedding_deployment="embedding",
            embedding_dimensions=2,
        )

        vector_store.create_store(config, Mock())

        signer.assert_called_once_with(credentials, "us-east-1", "aoss")
        settings = opensearch.call_args.kwargs
        self.assertEqual(
            settings["hosts"],
            [{"host": "collection.aoss.amazonaws.com", "port": 443}],
        )
        self.assertIs(settings["http_auth"], auth)
        self.assertTrue(settings["use_ssl"])
        self.assertTrue(settings["verify_certs"])


if __name__ == "__main__":
    unittest.main()
