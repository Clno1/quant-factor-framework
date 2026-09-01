"""Strict classification for market-data semantic drift recovery."""
from __future__ import annotations


def is_recoverable_semantic_drift(error: object) -> bool:
    """Return whether an incremental-history error requires a full rebuild.

    The writer emits these messages only after it has fetched successfully and
    compared overlapping canonical history. Provider, network, identity, and
    quality-gate failures intentionally remain fail-closed.
    """
    message = str(error or "").lower()
    if "full rebuild" not in message:
        return False
    return (
        ("non-uniform" in message and "revision" in message)
        or "no overlap anchor" in message
        or "inconsistent zero-volume overlap" in message
    )


__all__ = ["is_recoverable_semantic_drift"]
