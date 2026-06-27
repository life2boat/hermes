from __future__ import annotations

import logging
import os
import uuid
from urllib.parse import urljoin

import requests
from dataclasses import dataclass
from typing import Any, Callable

from gateway.memory.settings import env_flag
from gateway.memory.embedding_adapter import EmbeddingAdapter

logger = logging.getLogger(__name__)


class _RestQdrantClient:
    def __init__(self, *, url: str, api_key: str | None, timeout: float) -> None:
        self.url = url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        return headers

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.request(
            method,
            urljoin(self.url, path.lstrip("/")),
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    def get_collection(self, collection_name: str) -> dict[str, Any]:
        return self._request("GET", f"collections/{collection_name}")

    def create_collection(self, *, collection_name: str, vectors_config: Any) -> dict[str, Any]:
        if hasattr(vectors_config, "size") and hasattr(vectors_config, "distance"):
            distance = getattr(vectors_config.distance, "name", str(vectors_config.distance))
            vectors_payload = {"size": vectors_config.size, "distance": distance.title()}
        else:
            vectors_payload = dict(vectors_config)
        return self._request(
            "PUT",
            f"collections/{collection_name}",
            payload={"vectors": vectors_payload},
        )

    def upsert(self, *, collection_name: str, points: list[dict[str, Any]], wait: bool = False) -> dict[str, Any]:
        suffix = "?wait=true" if wait else "?wait=false"
        return self._request(
            "PUT",
            f"collections/{collection_name}/points{suffix}",
            payload={"points": points},
        )

    def search(
        self,
        *,
        collection_name: str,
        query_vector: list[float],
        query_filter: dict[str, Any],
        limit: int,
        with_payload: bool = True,
    ) -> list[dict[str, Any]]:
        data = self._request(
            "POST",
            f"collections/{collection_name}/points/search",
            payload={
                "vector": query_vector,
                "filter": query_filter,
                "limit": limit,
                "with_payload": with_payload,
            },
        )
        result = data.get("result")
        return result if isinstance(result, list) else []


@dataclass(slots=True)
class QdrantMemoryHit:
    sqlite_id: int | None
    payload: dict[str, Any]
    score: float | None = None


class QdrantMemoryAdapter:
    """Best-effort Qdrant adapter with strict timeouts and graceful degradation."""

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        collection_name: str | None = None,
        timeout: float = 1.5,
        vector_size: int = 32,
        embedding_adapter: EmbeddingAdapter | None = None,
        client_factory: Callable[[], Any] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.url = url or os.getenv("QDRANT_URL")
        self.api_key = api_key or os.getenv("QDRANT_API_KEY")
        self.collection_name = collection_name or os.getenv("QDRANT_COLLECTION") or "healbite_memory_os"
        self.timeout = timeout
        self.vector_size = vector_size
        self.embedding_adapter = embedding_adapter or EmbeddingAdapter(vector_size=vector_size)
        self._client_factory = client_factory
        self._enabled = env_flag("MEMORY_VECTOR_ENABLED", default=False) if enabled is None else enabled
        self._client: Any | None = None
        self._client_failed = False
        self._collection_ready = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _build_client(self) -> Any | None:
        if self._client_factory is not None:
            return self._client_factory()
        if not self.url:
            logger.info("QDRANT_URL is not configured; semantic search disabled")
            return None
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            logger.info("qdrant-client is not installed; using REST fallback for semantic search")
            return _RestQdrantClient(url=self.url, api_key=self.api_key, timeout=self.timeout)
        return QdrantClient(url=self.url, api_key=self.api_key, timeout=self.timeout)

    def _get_client(self) -> Any | None:
        if not self._enabled or self._client_failed:
            return None
        if self._client is not None:
            return self._client
        try:
            self._client = self._build_client()
        except Exception as exc:
            logger.warning("failed to initialize Qdrant client: error_type=%s", exc.__class__.__name__)
            self._client_failed = True
            return None
        if self._client is None:
            self._client_failed = True
        return self._client

    def _vector_config(self) -> Any:
        try:
            from qdrant_client import models
        except ImportError:
            return {"size": self.vector_size, "distance": "Cosine"}
        return models.VectorParams(size=self.vector_size, distance=models.Distance.COSINE)

    def ensure_collection(self) -> bool:
        if self._collection_ready:
            return True
        client = self._get_client()
        if client is None:
            return False
        try:
            get_collection = getattr(client, "get_collection", None)
            create_collection = getattr(client, "create_collection", None)
            if callable(get_collection):
                try:
                    get_collection(self.collection_name)
                    self._collection_ready = True
                    return True
                except Exception:
                    if callable(create_collection):
                        create_collection(
                            collection_name=self.collection_name,
                            vectors_config=self._vector_config(),
                        )
                        self._collection_ready = True
                        return True
                    raise
            if callable(create_collection):
                create_collection(
                    collection_name=self.collection_name,
                    vectors_config=self._vector_config(),
                )
            self._collection_ready = True
            return True
        except Exception as exc:
            logger.warning(
                "failed to ensure Qdrant collection: collection=%s error_type=%s",
                self.collection_name,
                exc.__class__.__name__,
            )
            return False

    def upsert_fact(
        self,
        *,
        sqlite_id: int,
        user_id: int,
        text: str,
        payload: dict[str, Any],
    ) -> bool:
        client = self._get_client()
        if client is None or not self.ensure_collection():
            return False
        vector = self.embedding_adapter.embed_text(text)
        point = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"healbite-memory:{user_id}:{sqlite_id}")),
            "vector": vector,
            "payload": {
                **payload,
                "sqlite_id": sqlite_id,
                "user_id": user_id,
            },
        }
        try:
            client.upsert(
                collection_name=self.collection_name,
                points=[point],
                wait=False,
            )
            return True
        except TypeError:
            try:
                client.upsert(collection_name=self.collection_name, points=[point])
                return True
            except Exception as exc:
                logger.warning("failed to upsert semantic memory point: error_type=%s", exc.__class__.__name__)
                return False
        except Exception as exc:
            logger.warning("failed to upsert semantic memory point: error_type=%s", exc.__class__.__name__)
            return False

    def search(self, *, query_text: str, user_id: int, limit: int = 5) -> list[QdrantMemoryHit]:
        client = self._get_client()
        if client is None or not self.ensure_collection():
            return []
        vector = self.embedding_adapter.embed_text(query_text)
        qdrant_filter = {
            "must": [
                {
                    "key": "user_id",
                    "match": {"value": user_id},
                }
            ]
        }
        try:
            raw_hits = client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                query_filter=qdrant_filter,
                limit=limit,
                with_payload=True,
            )
        except TypeError:
            raw_hits = client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                query_filter=qdrant_filter,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("Qdrant semantic search failed: %s", exc.__class__.__name__)
            return []

        hits: list[QdrantMemoryHit] = []
        for hit in raw_hits or []:
            payload = getattr(hit, "payload", None)
            if payload is None and isinstance(hit, dict):
                payload = hit.get("payload", {})
            if payload is None:
                payload = {}
            score = getattr(hit, "score", None)
            if score is None and isinstance(hit, dict):
                score = hit.get("score")
            sqlite_id = payload.get("sqlite_id")
            try:
                sqlite_id = int(sqlite_id) if sqlite_id is not None else None
            except (TypeError, ValueError):
                sqlite_id = None
            hits.append(QdrantMemoryHit(sqlite_id=sqlite_id, payload=dict(payload), score=score))
        return hits
