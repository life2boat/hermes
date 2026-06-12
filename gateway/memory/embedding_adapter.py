from __future__ import annotations

import hashlib
import math
import re
from typing import Callable, Sequence

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class EmbeddingAdapter:
    """Small embedding adapter with a deterministic local fallback.

    The bridge can inject a real provider-backed embedding callable later.
    Until then, this adapter stays runtime-safe by producing normalized
    hash-based vectors locally.
    """

    def __init__(
        self,
        embed_fn: Callable[[str], Sequence[float]] | None = None,
        *,
        vector_size: int = 32,
    ) -> None:
        if vector_size <= 0:
            raise ValueError("vector_size must be positive")
        self._embed_fn = embed_fn
        self.vector_size = vector_size

    def embed_text(self, text: str) -> list[float]:
        cleaned = (text or "").strip()
        if self._embed_fn is not None:
            return self._normalize([float(value) for value in self._embed_fn(cleaned)])
        return self._fallback_embed(cleaned)

    def _fallback_embed(self, text: str) -> list[float]:
        vector = [0.0] * self.vector_size
        tokens = _TOKEN_RE.findall(text.lower()) or [text.lower() or "<empty>"]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for index, byte in enumerate(digest):
                bucket = index % self.vector_size
                vector[bucket] += (byte / 255.0) - 0.5
        return self._normalize(vector)

    @staticmethod
    def _normalize(vector: Sequence[float]) -> list[float]:
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0.0:
            return [0.0 for _ in vector]
        return [value / magnitude for value in vector]
