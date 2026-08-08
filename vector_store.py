"""Azure AI Search storage and retrieval for structured review notes."""

import hashlib
import os

from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SearchableField,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery


def resolve_config(config: dict, environ: dict[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    enabled = env.get("AZURE_SEARCH_ENABLED", str(config.get("enabled", False))).lower()
    if enabled not in {"true", "false"}:
        raise ValueError("Azure AI Search enabled must be true or false")
    resolved = {
        "enabled": enabled == "true",
        "endpoint": env.get("AZURE_SEARCH_ENDPOINT") or config.get("endpoint"),
        "index_name": env.get("AZURE_SEARCH_INDEX") or config.get("index_name", "pr-review-notes"),
        "api_key": env.get("AZURE_SEARCH_API_KEY") or config.get("api_key"),
        "embedding_deployment": env.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
        or config.get("embedding_deployment"),
        "embedding_dimensions": int(
            env.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS", config.get("embedding_dimensions", 1536))
        ),
    }
    if resolved["enabled"]:
        missing = [
            key for key in ("endpoint", "embedding_deployment") if not resolved[key]
        ]
        if missing:
            raise ValueError(f"Azure AI Search requires: {', '.join(missing)}")
        if resolved["embedding_dimensions"] <= 0:
            raise ValueError("embedding_dimensions must be positive")
        resolved["endpoint"] = resolved["endpoint"].rstrip("/")
    return resolved


def _credential(api_key: str | None):
    return AzureKeyCredential(api_key) if api_key else DefaultAzureCredential()


def create_store(config: dict, embedding_client):
    credential = _credential(config.get("api_key"))
    index_client = SearchIndexClient(config["endpoint"], credential)
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(name="reviewer", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="repo", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="pr_number", type=SearchFieldDataType.Int64, filterable=True),
        SimpleField(name="github_comment_id", type=SearchFieldDataType.Int64, filterable=True),
        SimpleField(name="pr_url", type=SearchFieldDataType.String),
        SimpleField(name="file_path", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="category", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="severity", type=SearchFieldDataType.String, filterable=True),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=config["embedding_dimensions"],
            vector_search_profile_name="review-notes-profile",
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="review-notes-hnsw")],
        profiles=[VectorSearchProfile(
            name="review-notes-profile",
            algorithm_configuration_name="review-notes-hnsw",
        )],
    )
    index_client.create_or_update_index(
        SearchIndex(name=config["index_name"], fields=fields, vector_search=vector_search)
    )
    search_client = SearchClient(config["endpoint"], config["index_name"], credential)
    return AzureReviewNoteStore(
        search_client, embedding_client, config["embedding_deployment"]
    )


class AzureReviewNoteStore:
    def __init__(self, search_client, embedding_client, embedding_deployment: str):
        self._search_client = search_client
        self._embedding_client = embedding_client
        self._embedding_deployment = embedding_deployment

    @staticmethod
    def _content(note: dict) -> str:
        return "\n".join(
            value for value in (
                note["original_issue"],
                note["requested_change"],
                note["rationale"],
                note["original_body"],
            ) if value
        )

    def save_notes(self, notes: list[dict], reviewer: str) -> int:
        if not notes:
            return 0
        contents = [self._content(note) for note in notes]
        vectors = self._embedding_client.embed(self._embedding_deployment, contents)
        documents = []
        for note, content, vector in zip(notes, contents, vectors):
            source_identity = note.get("github_comment_id") or "|".join((
                str(note["pr_number"]), note.get("file_path") or "", note["original_body"]
            ))
            identity = f'{note["repo"]}|{note["comment_type"]}|{source_identity}'
            documents.append({
                "id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "content": content,
                "content_vector": vector,
                "reviewer": reviewer,
                "repo": note["repo"],
                "pr_number": note["pr_number"],
                "github_comment_id": note.get("github_comment_id"),
                "pr_url": note["pr_url"],
                "file_path": note.get("file_path"),
                "category": note["category"],
                "severity": note["severity"],
            })
        results = self._search_client.upload_documents(documents)
        failures = [result for result in results if not result.succeeded]
        if failures:
            raise RuntimeError(f"Azure AI Search rejected {len(failures)} review notes")
        return len(documents)

    def delete_notes(self, notes: list[dict]) -> int:
        keys = []
        for note in notes:
            source_identity = note.get("github_comment_id") or "|".join((
                str(note["pr_number"]), note.get("file_path") or "", note["original_body"]
            ))
            identity = f'{note["repo"]}|{note["comment_type"]}|{source_identity}'
            keys.append({"id": hashlib.sha256(identity.encode("utf-8")).hexdigest()})
        if not keys:
            return 0
        results = self._search_client.delete_documents(documents=keys)
        failures = [result for result in results if not result.succeeded]
        if failures:
            raise RuntimeError(f"Azure AI Search rejected {len(failures)} note deletions")
        return len(keys)

    def search(
        self, query: str, repo: str, limit: int = 5, reviewer: str | None = None
    ) -> list[dict]:
        if not query.strip():
            raise ValueError("search query must not be empty")
        if limit < 1:
            raise ValueError("search limit must be at least 1")
        vector = self._embedding_client.embed(self._embedding_deployment, [query])[0]
        vector_query = VectorizedQuery(vector=vector, k_nearest_neighbors=limit, fields="content_vector")
        escaped_repo = repo.replace("'", "''")
        filters = [f"repo eq '{escaped_repo}'"]
        if reviewer:
            escaped = reviewer.replace("'", "''")
            filters.append(f"reviewer eq '{escaped}'")
        results = self._search_client.search(
            search_text=query,
            vector_queries=[vector_query],
            filter=" and ".join(filters),
            top=limit,
            select=["content", "repo", "pr_number", "pr_url", "file_path", "category", "severity"],
        )
        return [dict(result) for result in results]