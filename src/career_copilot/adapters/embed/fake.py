"""A bag-of-words embedder for tests: deterministic, instant, and needs no model download."""

from __future__ import annotations

import math
import re
import zlib
from collections.abc import Sequence

_TOKEN = re.compile(r"\w+")


class FakeEmbedder:
    """Hashes lowercase tokens into `dim` buckets, then L2-normalises.

    Shared words give a positive cosine, which is all ranking tests need. `crc32` rather
    than `hash()`, because `hash()` is salted per process and would make vectors drift.
    """

    model = "fake-embed"

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            vec[zlib.crc32(token.encode()) % self.dim] += 1.0
        norm = math.sqrt(math.fsum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec
