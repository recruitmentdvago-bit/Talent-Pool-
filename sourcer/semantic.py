"""Lightweight semantic similarity - TF-IDF cosine over uni/bi-grams.

Deliberately dependency-free (numpy only) so the bot runs anywhere. If you
later install sentence-transformers, set SOURCER_EMBEDDINGS=st and this module
will use it instead; the scorer's interface does not change.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter

from .normalize import norm_text

STOP = set("""a an and are as at be by for from has have in is it its of on or that the to
was were will with this these those we you your our их i he she they them их but not no if
then than so such can could should would may might must about into over under more most
other some any each both few all own same very s t don now d ll m o re ve y ain job role
work working experience years year team company responsibilities requirements skills""".split())


BIGRAM_WEIGHT = 0.45  # bigrams rarely co-occur; full weight dilutes the cosine


def _terms(text: str) -> list[tuple[str, float]]:
    toks = [t for t in re.split(r"[^a-z0-9+#.]+", norm_text(text)) if t and t not in STOP and len(t) > 1]
    grams: list[tuple[str, float]] = [(t, 1.0) for t in toks]
    grams += [(f"{a}_{b}", BIGRAM_WEIGHT) for a, b in zip(toks, toks[1:])]
    return grams


class TfidfSpace:
    """Fit on a corpus, then compare any two documents in it."""

    def __init__(self, docs: list[str], min_df: int = 1):
        self.df: Counter[str] = Counter()
        for d in docs:
            self.df.update({t for t, _ in _terms(d)})
        self.n = max(1, len(docs))
        self.vocab = {t for t, c in self.df.items() if c >= min_df}
        self.idf = {
            t: math.log((self.n + 1) / (self.df[t] + 1)) + 1.0 for t in self.vocab
        }

    def vector(self, text: str) -> dict[str, float]:
        tf: Counter[str] = Counter()
        gw: dict[str, float] = {}
        for t, w in _terms(text):
            if t in self.vocab:
                tf[t] += 1
                gw[t] = w
        if not tf:
            return {}
        mx = max(tf.values())
        v = {t: (0.5 + 0.5 * c / mx) * self.idf[t] * gw[t] for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    @staticmethod
    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        if len(b) < len(a):
            a, b = b, a
        return max(0.0, min(1.0, sum(x * b.get(t, 0.0) for t, x in a.items())))

    def similarity(self, doc_a: str, doc_b: str) -> float:
        return self.cosine(self.vector(doc_a), self.vector(doc_b))


class SentenceTransformerSpace:  # pragma: no cover - optional path
    """Drop-in replacement when sentence-transformers is installed."""

    def __init__(self, docs: list[str], model: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model)

    def vector(self, text: str):
        return self.model.encode(text or "", normalize_embeddings=True)

    @staticmethod
    def cosine(a, b) -> float:
        import numpy as np
        if a is None or b is None:
            return 0.0
        return float(max(0.0, min(1.0, np.dot(a, b))))

    def similarity(self, a: str, b: str) -> float:
        return self.cosine(self.vector(a), self.vector(b))


def build_space(docs: list[str]):
    if os.environ.get("SOURCER_EMBEDDINGS") == "st":
        try:
            return SentenceTransformerSpace(docs)
        except Exception:
            pass
    return TfidfSpace(docs)
