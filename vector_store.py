"""AWS OpenSearch vector storage for structured review notes."""

import hashlib
import os

import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from application_ports import ModelClient


def resolve_config(
    config: dict,
    environ: dict[str, str] | None = None,
    *,
    embedding_deployment: str | None = None,
    embedding_dimensions: int = 1536,
) -> dict:
    env = os.environ if environ is None else environ
    enabled = env.get("AWS_OPENSEARCH_ENABLED", str(config.get("enabled", False))).lower()
    if enabled not in {"true", "false"}:
        raise ValueError("AWS OpenSearch enabled must be true or false")
    resolved = {
        "enabled": enabled == "true",
        "endpoint": env.get("AWS_OPENSEARCH_ENDPOINT") or config.get("endpoint"),
        "index_name": env.get("AWS_OPENSEARCH_INDEX")
        or config.get("index_name", "pr-review-notes"),
        "region": env.get("AWS_REGION") or config.get("region", "us-east-1"),
        "service": env.get("AWS_OPENSEARCH_SERVICE") or config.get("service", "aoss"),
        "embedding_deployment": embedding_deployment,
        "embedding_dimensions": embedding_dimensions,
    }
    if resolved["enabled"]:
        missing = [key for key in ("endpoint", "embedding_deployment") if not resolved[key]]
        if missing:
            raise ValueError(f"AWS OpenSearch requires: {', '.join(missing)}")
        if embedding_dimensions <= 0:
            raise ValueError("embedding_dimensions must be positive")
        resolved["endpoint"] = resolved["endpoint"].replace("https://", "").rstrip("/")
    return resolved


def create_store(config: dict, embedding_client: ModelClient):
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise ValueError("AWS credentials are required for OpenSearch")
    auth = AWSV4SignerAuth(credentials, config["region"], config["service"])
    search_client = OpenSearch(
        hosts=[{"host": config["endpoint"], "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=60,
        max_retries=2,
        retry_on_timeout=True,
    )
    return OpenSearchReviewNoteStore(
        search_client,
        embedding_client,
        config["embedding_deployment"],
        config["index_name"],
        config["embedding_dimensions"],
    )


class OpenSearchReviewNoteStore:
    def __init__(
        self,
        search_client,
        embedding_client: ModelClient,
        embedding_deployment: str,
        index_name: str = "pr-review-notes",
        embedding_dimensions: int = 1536,
    ):
        self._search_client = search_client
        self._embedding_client = embedding_client
        self._embedding_deployment = embedding_deployment
        self._index_name = index_name
        if not self._search_client.indices.exists(index=index_name):
            self._search_client.indices.create(index=index_name, body={
                "settings": {"index": {"knn": True}},
                "mappings": {"properties": {
                    "content": {"type": "text"},
                    "content_vector": {
                        "type": "knn_vector",
                        "dimension": embedding_dimensions,
                        "method": {
                            "name": "hnsw",
                            "engine": "faiss",
                            "space_type": "cosinesimil",
                        },
                    },
                    "reviewer": {"type": "keyword"},
                    "repo": {"type": "keyword"},
                    "pr_number": {"type": "long"},
                    "github_comment_id": {"type": "long"},
                    "pr_url": {"type": "keyword", "index": False},
                    "file_path": {"type": "keyword"},
                    "category": {"type": "keyword"},
                    "severity": {"type": "keyword"},
                    "merged_at": {"type": "date"},
                }},
            })

    @staticmethod
    def _content(note: dict) -> str:
        return "\n".join(value for value in (
            note["original_issue"],
            note["requested_change"],
            note["rationale"],
            note["original_body"],
            note.get("implementation_example", ""),
        ) if value)

    @staticmethod
    def _id(note: dict) -> str:
        source_identity = note.get("github_comment_id") or "|".join((
            str(note["pr_number"]), note.get("file_path") or "", note["original_body"]
        ))
        identity = f'{note["repo"]}|{note["comment_type"]}|{source_identity}'
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def save_notes(self, notes: list[dict]) -> int:
        if not notes:
            return 0
        contents = [self._content(note) for note in notes]
        vectors = self._embedding_client.embed(self._embedding_deployment, contents)
        body = []
        for note, content, vector in zip(notes, contents, vectors):
            body.extend((
                {"index": {"_index": self._index_name, "_id": self._id(note)}},
                {
                    "content": content,
                    "content_vector": vector,
                    "reviewer": note["reviewer"],
                    "repo": note["repo"],
                    "pr_number": note["pr_number"],
                    "github_comment_id": note.get("github_comment_id"),
                    "pr_url": note["pr_url"],
                    "file_path": note.get("file_path"),
                    "category": note["category"],
                    "severity": note["severity"],
                    "merged_at": note.get("merged_at") or None,
                },
            ))
        response = self._search_client.bulk(body=body, refresh=True)
        if response.get("errors"):
            raise RuntimeError("AWS OpenSearch rejected one or more review notes")
        return len(notes)

    def delete_notes(self, notes: list[dict]) -> int:
        if not notes:
            return 0
        body = [
            {"delete": {"_index": self._index_name, "_id": self._id(note)}}
            for note in notes
        ]
        response = self._search_client.bulk(body=body, refresh=True)
        failures = [
            item for item in response.get("items", [])
            if item.get("delete", {}).get("status") not in {200, 404}
        ]
        if response.get("errors") and failures:
            raise RuntimeError("AWS OpenSearch rejected one or more note deletions")
        return len(notes)

    def replace_notes(self, notes: list[dict], repos: list[str]) -> int:
        if not repos:
            raise ValueError("At least one repository is required for vector replacement")
        count = self.save_notes(notes)
        selected_ids = [self._id(note) for note in notes]
        query: dict = {"bool": {"filter": [{"terms": {"repo": repos}}]}}
        if selected_ids:
            query["bool"]["must_not"] = [{"ids": {"values": selected_ids}}]
        response = self._search_client.delete_by_query(
            index=self._index_name,
            body={"query": query},
            refresh=True,
            conflicts="proceed",
        )
        if response.get("failures"):
            raise RuntimeError("AWS OpenSearch failed to clear prior review notes")
        return count

    def search(
        self, query: str, repo: str, limit: int = 5,
        reviewers: list[str] | None = None,
    ) -> list[dict]:
        if not query.strip():
            raise ValueError("search query must not be empty")
        if limit < 1:
            raise ValueError("search limit must be at least 1")
        vector = self._embedding_client.embed(self._embedding_deployment, [query])[0]
        filters = [{"term": {"repo": repo}}]
        if reviewers:
            filters.append({"terms": {"reviewer": reviewers}})
        response = self._search_client.search(index=self._index_name, body={
            "size": limit,
            "_source": [
                "content", "repo", "pr_number", "pr_url", "file_path", "category", "severity"
            ],
            "query": {"knn": {"content_vector": {
                "vector": vector,
                "k": limit,
                "filter": {"bool": {"filter": filters}},
            }}},
        })
        return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]
