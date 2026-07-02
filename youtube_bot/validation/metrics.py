from __future__ import annotations

import math

import numpy as np

from youtube_bot.utils.helpers import normalize_text


POSITIVE_WORDS = {
    "bom",
    "boa",
    "amei",
    "top",
    "legal",
    "incrivel",
    "kkkk",
    "haha",
    "valeu",
}
NEGATIVE_WORDS = {
    "ruim",
    "odio",
    "chato",
    "bug",
    "erro",
    "nao gostei",
    "horrivel",
    "lixo",
}


def simple_sentiment_score(text: str) -> float:
    normalized = normalize_text(text)
    positive = sum(1 for word in POSITIVE_WORDS if word in normalized)
    negative = sum(1 for word in NEGATIVE_WORDS if word in normalized)
    if positive == negative == 0:
        return 0.0
    return (positive - negative) / max(positive + negative, 1)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if math.isclose(denom, 0.0):
        return 0.0
    return float(np.dot(va, vb) / denom)
