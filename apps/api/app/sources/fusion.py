"""Reciprocal rank fusion shared by hybrid retrieval and federated search.

RRF merges several ranked lists without needing their scores to be comparable. That property
matters twice here: PostgreSQL full-text ranks cannot be compared with pgvector cosine
distances, and no two upstream sources score relevance the same way either.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence

# The conventional damping constant from the original RRF paper. It flattens the contribution
# of top ranks so a single list cannot dominate the merged ordering.
DEFAULT_RRF_K = 60


def reciprocal_rank_score(rank: int, *, k: int = DEFAULT_RRF_K) -> float:
    """Score a single 1-indexed rank position."""
    if rank < 1:
        raise ValueError("rank must be 1-indexed")
    return 1.0 / (k + rank)


def fuse_rankings[ItemT: Hashable](
    rankings: Iterable[Sequence[ItemT]],
    *,
    k: int = DEFAULT_RRF_K,
) -> dict[ItemT, float]:
    """Fuse ranked lists into one score per item.

    Each list is treated as an independent opinion. An item appearing in several lists
    accumulates their contributions, which is what lets agreement across sources outrank a
    single source's confident-looking top hit. Duplicates within one list are ignored so a
    list cannot inflate an item by repeating it.
    """
    if k < 1:
        raise ValueError("k must be positive")
    scores: dict[ItemT, float] = {}
    for ranking in rankings:
        seen: set[ItemT] = set()
        rank = 0
        for item in ranking:
            if item in seen:
                continue
            seen.add(item)
            rank += 1
            scores[item] = scores.get(item, 0.0) + reciprocal_rank_score(rank, k=k)
    return scores


def rank_by_fused_score[ItemT: Hashable](
    scores: dict[ItemT, float],
    *,
    limit: int,
    tiebreak: bool = True,
) -> list[ItemT]:
    """Return the highest-scoring items, most relevant first.

    Ties are broken by the item's string form so results stay stable across runs; unstable
    ordering makes retrieval quality impossible to measure.
    """
    if limit < 0:
        raise ValueError("limit must not be negative")
    if tiebreak:
        ordered = sorted(scores, key=lambda item: (-scores[item], str(item)))
    else:
        ordered = sorted(scores, key=lambda item: -scores[item])
    return ordered[:limit]
