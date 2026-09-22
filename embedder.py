"""Embedding wrapper. One place decides the model, the prefix and normalisation.

Verified empirically on fastembed 0.4.2: query_embed() returns vectors identical
to embed() for BAAI/bge-small-en-v1.5, i.e. it does NOT apply the BGE instruction
prefix. The application therefore applies it, and records that fact in the
manifest so the app can assert the contract at startup.
"""
from __future__ import annotations

import numpy as np

from config import settings

_model = None


def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=settings.MODEL_NAME)
    return _model


def _to_matrix(vectors) -> np.ndarray:
    arr = np.asarray(list(vectors), dtype="float32")
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return arr


def embed_passages(texts: list[str]) -> np.ndarray:
    """Index side. No prefix, per the BGE model card."""
    if not texts:
        return np.zeros((0, settings.EMBED_DIM), dtype="float32")
    return _to_matrix(get_model().embed(texts, batch_size=settings.EMBED_BATCH))


def embed_queries(texts: list[str]) -> np.ndarray:
    """Query side. Prefix applied here and nowhere else."""
    if not texts:
        return np.zeros((0, settings.EMBED_DIM), dtype="float32")
    prefixed = [settings.QUERY_PREFIX + t for t in texts]
    return _to_matrix(get_model().embed(prefixed, batch_size=settings.EMBED_BATCH))


def assert_normalised(mat: np.ndarray) -> None:
    """FAISS inner product only equals cosine similarity for unit vectors. If
    this ever fails, every threshold in settings becomes meaningless."""
    if mat.size == 0:
        return
    norms = np.linalg.norm(mat, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-4):
        raise RuntimeError(
            f"Embeddings are not L2-normalised (min={norms.min():.5f}, "
            f"max={norms.max():.5f}). FAISS inner product would not be cosine."
        )
