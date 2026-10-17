"""FastEmbed: ONNX Runtime on the CPU, so ranking never competes with the LLM for VRAM.

The model downloads once into FastEmbed's cache on first use, then loads from disk.
"""

from __future__ import annotations

from collections.abc import Sequence


class FastEmbedEmbedder:
    """The production `Embedder`. Building one loads the model, so build it once per run."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        # Deferred: importing fastembed pulls in onnxruntime, which every other command
        # would otherwise pay for at startup.
        from fastembed import TextEmbedding

        self.model = model
        self._engine = TextEmbedding(model_name=model)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self._engine.embed(list(texts))]
